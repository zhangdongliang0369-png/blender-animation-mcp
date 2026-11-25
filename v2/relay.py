"""Private outbound-worker protocol, separate from user-facing MCP credentials."""
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import shutil
import stat
import tempfile
import time
from zipfile import BadZipFile, ZIP_STORED, ZipFile

from starlette.background import BackgroundTask
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from .isolation import WorkerError
from .store import MIB
from .tasks import collect_outputs, finish_job

LEASE_SECONDS = 45
INPUT_MIB = 70
OUTPUT_NAME = re.compile(r"(?:environment|model-report|progress|worker-metrics)\.json|scene\.blend|converted\.glb|preview\.mp4|recipe\.py|execution\.log|(?:frame|seq)-[0-9]+\.png")
ID = re.compile(r"[0-9a-f]{32}")


def write_package(root: Path, destination: Path):
    with ZipFile(destination, "w", compression=ZIP_STORED) as zip_file:
        for path in sorted(root.rglob("*")):
            if path.is_file() and not path.is_symlink():
                zip_file.write(path, path.relative_to(root).as_posix())


def unpack_package(package: Path, target: Path, max_bytes: int, *, output: bool):
    with ZipFile(package) as z:
        entries = z.infolist()
        if len(entries) > 270 or sum(entry.file_size for entry in entries) > max_bytes:
            raise ValueError("Worker package exceeds the allowed size")
        for entry in entries:
            name = entry.filename
            parts = name.split("/")
            if (
                entry.compress_type != ZIP_STORED or entry.is_dir() or "\\" in name
                or any(not part or part in {".", ".."} or part.startswith(".") for part in parts)
                or stat.S_ISLNK(entry.external_attr >> 16)
                or (output and not OUTPUT_NAME.fullmatch(name))
                or (not output and name not in {"job.json", "project.blend"} and not name.startswith("assets/"))
            ):
                raise ValueError("Invalid worker package entry")
            path = target / name
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with z.open(entry) as source, path.open("xb") as result:
                shutil.copyfileobj(source, result, 256 * 1024)


class Relay:
    def __init__(self, tasks, key: str):
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", key):
            raise ValueError("A separate high-entropy worker token is required")
        self.tasks = tasks
        self.store = tasks.store
        self.key = "Bearer " + key
        self.recipe = hashlib.sha256(tasks.sandbox.script.read_bytes()).hexdigest()
        with self.store.connection() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS worker_leases(
                    job TEXT PRIMARY KEY REFERENCES jobs(id), token_hash TEXT NOT NULL, expires REAL NOT NULL)
            """)

    def mount(self, app):
        app.routes.extend([
            Route("/v2/worker/claim", self.claim, methods=["POST"]),
            Route("/v2/worker/{job}/heartbeat", self.heartbeat, methods=["POST"]),
            Route("/v2/worker/{job}/finish", self.finish, methods=["PUT"]),
        ])

    def authenticated(self, request):
        return hmac.compare_digest(request.headers.get("authorization", ""), self.key)

    def reap(self):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """UPDATE jobs SET status='interrupted', ended=?, error_code='WORKER_LEASE_EXPIRED',
                   error='Remote worker stopped renewing the execution lease'
                   WHERE status='running' AND id IN (SELECT job FROM worker_leases WHERE expires<?)""",
                (time.time(), time.time()),
            )
            db.execute("DELETE FROM worker_leases WHERE expires<?", (time.time(),))

    async def claim(self, request):
        if not self.authenticated(request):
            return Response(status_code=401)
        if request.headers.get("x-worker-recipe") != self.recipe:
            return JSONResponse({"error": "Worker recipe revision differs from control service"}, status_code=409)
        self.reap()
        token = secrets.token_urlsafe(32)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM worker_leases LIMIT 1").fetchone():
                return Response(status_code=204)
            row = db.execute(
                """SELECT * FROM jobs WHERE status='queued'
                   AND json_extract(spec,'$.operation')<>'export' ORDER BY created LIMIT 1"""
            ).fetchone()
            if not row:
                return Response(status_code=204)
            job = dict(row)
            db.execute("UPDATE jobs SET status='running', started=? WHERE id=?", (time.time(), job["id"]))
            db.execute("INSERT INTO worker_leases VALUES (?,?,?)", (
                job["id"], hashlib.sha256(token.encode()).hexdigest(), time.time() + LEASE_SECONDS,
            ))
        folder = self.store.job_dir(job["project"], job["id"])
        package = folder / "worker-input.zip"
        try:
            self.tasks.stage(folder, job, json.loads(job["spec"]))
            write_package(folder / "inputs", package)
            if package.stat().st_size > INPUT_MIB * MIB:
                raise ValueError("Worker input bundle exceeds the limit")
        except (ValueError, OSError) as exc:
            with self.store.connection() as db:
                finish_job(db, job, "failed", "WORKER_INPUT", str(exc), [], None)
                db.execute("DELETE FROM worker_leases WHERE job=?", (job["id"],))
            package.unlink(missing_ok=True)
            return JSONResponse({"error": "Task could not be staged"}, status_code=422)
        finally:
            shutil.rmtree(folder / "inputs", ignore_errors=True)
        return FileResponse(package, media_type="application/zip", background=BackgroundTask(package.unlink, missing_ok=True),
                            headers={"X-Worker-Job": job["id"], "X-Worker-Lease": token, "Cache-Control": "no-store"})

    def leased_job(self, db, request):
        ident = request.path_params["job"]
        token = request.headers.get("x-worker-lease", "")
        if not ID.fullmatch(ident) or len(token) != 43:
            return None
        row = db.execute(
            """SELECT jobs.*,worker_leases.token_hash FROM jobs JOIN worker_leases ON worker_leases.job=jobs.id
               WHERE jobs.id=? AND jobs.status='running' AND worker_leases.expires>?""",
            (ident, time.time()),
        ).fetchone()
        supplied = hashlib.sha256(token.encode()).hexdigest()
        return dict(row) if row and hmac.compare_digest(supplied, row["token_hash"]) else None

    async def heartbeat(self, request):
        if not self.authenticated(request):
            return Response(status_code=401)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            job = self.leased_job(db, request)
            if not job:
                return Response(status_code=409)
            db.execute("UPDATE worker_leases SET expires=? WHERE job=?", (time.time() + LEASE_SECONDS, job["id"]))
            progress_count = request.headers.get("x-worker-frames-done", "")
            total = len(json.loads(job["spec"]).get("frames", []))
            if progress_count.isdecimal() and len(progress_count) <= 3 and 0 <= int(progress_count) <= total:
                folder = self.store.job_dir(job["project"], job["id"])
                (folder / "remote-progress.json").write_text(json.dumps({"done": int(progress_count), "total": total}))
        return JSONResponse({"cancel": job["error_code"] == "CANCEL_REQUESTED"})

    async def finish(self, request):
        if not self.authenticated(request):
            return Response(status_code=401)
        with self.store.connection() as db:
            job = self.leased_job(db, request)
            if not job:
                return Response(status_code=409)
        folder = self.store.job_dir(job["project"], job["id"])
        with tempfile.TemporaryDirectory(dir=folder) as tmp:
            scratch = Path(tmp)
            package, out = scratch / "out.zip", scratch / "out"
            out.mkdir(mode=0o700)
            limit = (self.tasks.sandbox.disk_mib + 2) * MIB
            written = 0
            with package.open("wb") as stream:
                async for chunk in request.stream():
                    written += len(chunk)
                    if written > limit:
                        return Response(status_code=413)
                    stream.write(chunk)
            status = request.headers.get("x-worker-status", "")
            error_code = request.headers.get("x-worker-error", "WORKER_FAILED")
            if status not in {"completed", "failed", "cancelled"} or not re.fullmatch(r"[A-Z_]{2,40}", error_code):
                return Response(status_code=422)
            outputs, version, error = [], None, None
            try:
                unpack_package(package, out, limit, output=True)
                spec = json.loads(job["spec"])
                if status == "completed":
                    outputs, version = collect_outputs(spec, out)
                    actual = json.loads((out / "environment.json").read_text())["blender"]
                    if not actual.startswith(spec["blender_series"] + "."):
                        raise ValueError("Worker Blender version did not match the request")
                else:
                    log = out / "execution.log"
                    if log.is_file():
                        with log.open("rb") as stream:
                            stream.seek(max(0, log.stat().st_size - 4000))
                            error = stream.read().decode(errors="replace")
            except (BadZipFile, WorkerError, ValueError, OSError, KeyError):
                return Response(status_code=422)
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                current = self.leased_job(db, request)
                if not current:
                    return Response(status_code=409)
                if current["error_code"] == "CANCEL_REQUESTED":
                    status, error_code, error = "cancelled", "CANCELLED", "Remote worker stopped after cancellation"
                out.replace(folder / "artifacts")
                finish_job(db, current, status, None if status == "completed" else error_code, error, outputs, version)
                db.execute("DELETE FROM worker_leases WHERE job=?", (job["id"],))
        return Response(status_code=204)
