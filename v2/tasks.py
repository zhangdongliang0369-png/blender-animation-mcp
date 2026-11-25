"""Versioned task execution and output publication."""
import asyncio
import contextlib
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import time
from uuid import uuid4
import zipfile

from .isolation import Sandbox, WorkerError
from .store import canonical, sha256

logger = logging.getLogger(__name__)
VERSIONED = {"import", "product_showcase", "edit_objects"}


class TaskRunner:
    def __init__(self, store):
        self.store = store
        self.sandbox = Sandbox()
        self.event = asyncio.Event()
        self.dispatcher = None
        self.lockfile = None

    async def start(self):
        self.lockfile = (self.store.root / "worker.lock").open("a")
        try:
            fcntl.flock(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.lockfile.close()
            raise RuntimeError("The production storage already has an active worker") from exc
        with self.store.connection() as db:
            db.execute(
                """UPDATE jobs SET status='interrupted', ended=?, error_code='WORKER_RESTART',
                   error='Worker restarted before the task finished'
                   WHERE status='running'""", (time.time(),),
            )
        self.dispatcher = asyncio.create_task(self.dispatch())

    async def stop(self):
        if self.dispatcher:
            self.dispatcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.dispatcher
        if self.lockfile:
            self.lockfile.close()

    def enqueue(self, project, request, spec):
        task = self.store.enqueue(project, request, spec)
        self.event.set()
        return task

    async def dispatch(self):
        while True:
            self.event.clear()
            with self.store.connection() as db:
                row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
                if row:
                    db.execute("UPDATE jobs SET status='running',started=? WHERE id=?", (time.time(), row["id"]))
            if row:
                await self.execute(dict(row))
            else:
                await self.event.wait()

    def cancel(self, task):
        with self.store.connection() as db:
            changed = db.execute(
                """UPDATE jobs SET status='cancelled',ended=?,error_code='CANCELLED',
                   error='Cancelled before execution' WHERE id=? AND status='queued'""",
                (time.time(), task["id"]),
            ).rowcount
            if not changed:
                row = db.execute("SELECT status FROM jobs WHERE id=?", (task["id"],)).fetchone()
                if row["status"] != "running":
                    return False
                # Status remains running until the worker has actually stopped.
                db.execute("UPDATE jobs SET error_code='CANCEL_REQUESTED' WHERE id=?", (task["id"],))
        return True

    def stage(self, group, job, spec):
        inputs = group / "inputs"
        outputs = group / "artifacts"
        inputs.mkdir(mode=0o700, parents=True)
        outputs.mkdir(mode=0o700)
        for asset_id in spec.get("asset_ids", []):
            item = self.store.asset(job["project"], asset_id)
            dst = inputs / "assets" / item["name"]
            dst.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if dst.exists():
                raise ValueError("Multiple versions of the same input path were submitted")
            shutil.copyfile(self.store.asset_file(item), dst)
        if spec.get("base"):
            base = self.store.version(job["project"], spec["base"])
            path = self.store.job_dir(job["project"], base["task"]) / "artifacts" / "scene.blend"
            shutil.copyfile(path, inputs / "project.blend")
        (inputs / "job.json").write_text(canonical(spec), encoding="utf-8")
        return outputs

    async def execute(self, job):
        spec = json.loads(job["spec"])
        folder = self.store.job_dir(job["project"], job["id"])
        started = time.monotonic()
        error_code, error, outputs, version = None, None, [], None
        status = "failed"

        def cancelled():
            row = self.store.one("SELECT error_code FROM jobs WHERE id=?", (job["id"],))
            return bool(row and row["error_code"] == "CANCEL_REQUESTED")

        def remaining():
            seconds = int(spec["timeout"] - (time.monotonic() - started))
            if seconds <= 0:
                raise WorkerError("TIMEOUT", "Task exceeded its execution time limit")
            if cancelled():
                raise WorkerError("CANCELLED", "Task cancelled")
            return seconds

        try:
            out = self.stage(folder, job, spec)
            with (out / "execution.log").open("w+b") as log:
                if spec["operation"] == "export":
                    self.export_project(job, out)
                else:
                    blender = [
                        "@blender", "-b", "--factory-startup", "--disable-autoexec", "-t", "2",
                        "--python-exit-code", "1", "--python", "/worker.py",
                    ]
                    source = self.sandbox.script.read_bytes()
                    if spec.get("recipe_sha256") and hashlib.sha256(source).hexdigest() != spec["recipe_sha256"]:
                        raise WorkerError("RECIPE_CHANGED", "The approved recipe version has changed; resubmit explicitly")
                    if spec["operation"] in VERSIONED:
                        (out / "recipe.py").write_bytes(source)
                    if spec.get("cad"):
                        await self.sandbox.run(folder, ["@cad-python", "/cad_worker.py"], remaining(), log, cancelled)
                    await self.sandbox.run(folder, blender, remaining(), log, cancelled)
                    if spec["operation"] == "render_animation":
                        expected = [out / f"seq-{i:06d}.png" for i in range(1, len(spec["frames"]) + 1)]
                        if not all(path.is_file() for path in expected):
                            raise WorkerError("MISSING_FRAMES", "Blender did not output every requested frame")
                        fps = spec["render"]["fps"]
                        await self.sandbox.run(folder, [
                            "/usr/bin/ffmpeg", "-nostdin", "-loglevel", "error", "-threads", "1",
                            "-framerate", str(fps), "-i", "/output/seq-%06d.png",
                            "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p",
                            "-movflags", "+faststart", "/output/preview.mp4",
                        ], remaining(), log, cancelled)
                remaining()
            required = {
                "import": ["scene.blend", "model-report.json"],
                "product_showcase": ["scene.blend", "recipe.py"],
                "edit_objects": ["scene.blend", "recipe.py"],
                "inspect": ["model-report.json"],
                "probe": ["environment.json"] + (["frame-1.png"] if spec.get("render") else []),
                "render_animation": ["preview.mp4"],
                "render_image": [f"frame-{n}.png" for n in spec.get("frames", [])],
                "extract_frames": [f"frame-{n}.png" for n in spec.get("frames", [])],
                "export": ["project.zip"],
            }[spec["operation"]]
            for name in required:
                path = out / name
                if path.is_symlink() or not path.is_file() or not path.stat().st_size:
                    raise WorkerError("MISSING_OUTPUT", f"Expected output is missing: {name}")
            outputs = [{
                "name": p.name, "bytes": p.stat().st_size, "sha256": sha256(p),
            } for p in sorted(out.iterdir()) if p.is_file() and not p.is_symlink() and p.name != "progress.json"]
            if spec["operation"] in VERSIONED:
                version = {
                    "id": uuid4().hex, "parent": spec.get("base"),
                    "hash": sha256(out / "scene.blend"), "blender": json.loads((out / "environment.json").read_text())["blender"],
                }
            status = "completed"
        except WorkerError as exc:
            error_code, error = exc.code, str(exc)
            if exc.code == "CANCELLED":
                status = "cancelled"
        except asyncio.CancelledError:
            status, error_code, error = "interrupted", "WORKER_RESTART", "Worker stopped during execution"
            raise
        except Exception as exc:
            error_code, error = "TASK_ERROR", str(exc)[-2000:]
            logger.exception("Production task failed")
        finally:
            with self.store.connection() as db:
                if version and status == "completed":
                    db.execute("INSERT INTO versions VALUES (?,?,?,?,?,?,?)", (
                        version["id"], job["project"], job["id"], version["parent"],
                        time.time(), version["hash"], version["blender"],
                    ))
                db.execute(
                    "UPDATE jobs SET status=?,ended=?,error_code=?,error=?,outputs=?,version=? WHERE id=?",
                    (
                        status, time.time(), error_code, error[:4000] if error else None,
                        canonical(outputs) if status == "completed" else None,
                        version["id"] if version and status == "completed" else None, job["id"],
                    ),
                )
            shutil.rmtree(folder / "inputs", ignore_errors=True)

    def export_project(self, job, out):
        project = job["project"]
        data = {
            "project": self.store.require_project(project),
            "assets": self.store.all("SELECT * FROM assets WHERE project=?", (project,)),
            "versions": self.store.all("SELECT * FROM versions WHERE project=?", (project,)),
            "tasks": self.store.all("SELECT * FROM jobs WHERE project=? AND id<>?", (project, job["id"])),
        }
        files = {}
        for item in data["assets"]:
            files[f"source_assets/{item['id']}/{item['name']}"] = self.store.asset_file(item)
        for item in data["tasks"]:
            group = self.store.job_dir(project, item["id"]) / "artifacts"
            for entry in group.glob("*"):
                if entry.is_file() and not entry.is_symlink():
                    files[f"tasks/{item['id']}/{entry.name}"] = entry
        if sum(path.stat().st_size for path in files.values()) > self.sandbox.disk_mib * 1024 * 1024:
            raise WorkerError("EXPORT_LIMIT", "Project export exceeds the configured job disk budget")
        with zipfile.ZipFile(out / "project.zip", "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("manifest.json", canonical(data))
            for name, path in files.items():
                archive.write(path, name)
