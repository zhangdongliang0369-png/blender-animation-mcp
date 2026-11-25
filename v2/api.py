"""Authenticated project tools and scoped file transfer routes."""
from contextlib import asynccontextmanager
import hashlib
import html
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
from typing import Any, Literal
from uuid import UUID

from mcp.server.fastmcp import Image
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.routing import Route

from .models import Edit, Engine, Frame, Name, RenderSettings, Showcase
from .relay import Relay
from .store import DEPENDENCIES, MIB, MODELS, Store, project_path
from .tasks import TaskRunner

VERSION = "2.0.0-dev"
RECIPE_VERSION = "2026-11-v1"
HEADER = {
    "Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def kind_is_valid(path, name):
    suffix = Path(name).suffix.lower()
    with path.open("rb") as stream:
        front = stream.read(65536)
    if not front:
        return False
    if suffix in {".blend", ".blend1"}:
        return front.startswith((b"BLENDER", b"\x28\xb5\x2f\xfd", b"\x1f\x8b"))
    if suffix == ".glb":
        return len(front) >= 12 and front.startswith(b"glTF") and int.from_bytes(front[8:12], "little") == path.stat().st_size
    if suffix == ".gltf":
        try:
            return isinstance(json.loads(path.read_text()).get("asset"), dict)
        except (UnicodeError, ValueError, AttributeError):
            return False
    if suffix == ".fbx":
        return front.startswith(b"Kaydara FBX Binary") or b"FBXHeaderExtension:" in front
    if suffix == ".stl":
        return (len(front) >= 84 and 84 + 50 * int.from_bytes(front[80:84], "little") == path.stat().st_size) or (
            front.lstrip().startswith(b"solid") and b"facet normal" in front
        )
    if suffix in {".step", ".stp"}:
        return front.lstrip().startswith(b"ISO-10303-21;")
    if suffix == ".png":
        return front.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in {".jpg", ".jpeg"}:
        return front.startswith(b"\xff\xd8\xff")
    if suffix == ".exr":
        return front.startswith(b"v/1\x01")
    if suffix == ".hdr":
        return front.startswith((b"#?RADIANCE", b"#?RGBE"))
    if suffix == ".obj":
        return re.search(rb"(?m)^[\t ]*(?:v|f)[\t ]", front) is not None and b"\x00" not in front
    if suffix == ".mtl":
        return re.search(rb"(?m)^[\t ]*newmtl[\t ]", front) is not None
    return suffix == ".bin"


class Production:
    def __init__(self, mcp, origin, access_id, v1_running):
        signing_key = os.environ["BLENDER_V2_SIGNING_KEY"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", access_id) or len(signing_key) < 32:
            raise ValueError("Production requires a strong MCP URL credential and a separate signing key")
        if secrets.compare_digest(signing_key, access_id):
            raise ValueError("Production signing key must differ from the MCP access credential")
        owner = os.environ.get("BLENDER_V2_PRINCIPAL", "workspace-owner")
        self.store = Store(Path(os.environ["BLENDER_V2_DIR"]), owner, signing_key)
        worker_key = os.environ.get("BLENDER_V2_WORKER_KEY")
        if worker_key and any(secrets.compare_digest(worker_key, value) for value in (access_id, signing_key)):
            raise ValueError("Worker credentials must be separate")
        self.tasks = TaskRunner(self.store, remote=bool(worker_key))
        self.relay = Relay(self.tasks, worker_key) if worker_key else None
        self.origin = origin
        self.v1_running = v1_running
        self.upload_mib = int(os.environ.get("BLENDER_V2_UPLOAD_MIB", "32"))
        self.supported_series = os.environ.get("BLENDER_V2_SERIES", "5.3")
        self.register(mcp)

    def mount(self, app):
        existing = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(instance):
            async with existing(instance):
                await self.tasks.start()
                try:
                    yield
                finally:
                    await self.tasks.stop()

        app.router.lifespan_context = lifespan
        if self.relay:
            self.relay.mount(app)
        app.routes.append(Route("/v2/upload/{token}", self.transfer, methods=["GET", "PUT"]))
        app.routes.append(Route("/v2/download/{token}", self.download, methods=["GET"]))

    def url(self, purpose, **fields):
        token = self.store.sign(purpose, fields)
        return f"{self.origin}/v2/{purpose}/{token}"

    def task_data(self, row):
        spec = json.loads(row["spec"])
        data = {
            "task_id": row["id"], "project_id": row["project"], "status": row["status"],
            "operation": spec["operation"], "phase": row["status"], "created_at": row["created"],
            "started_at": row["started"], "ended_at": row["ended"], "base_project_version": spec.get("base"),
            "project_version": row["version"], "asset_ids": spec.get("asset_ids", []),
            "recipe": spec.get("recipe"), "recipe_sha256": spec.get("recipe_sha256"),
            "blender_requirement": spec.get("blender_series"),
            "error_code": row["error_code"], "error": row["error"],
            "retryable": row["status"] in {"failed", "interrupted", "cancelled"},
            "outputs": json.loads(row["outputs"]) if row["outputs"] else [],
        }
        folder = self.store.job_dir(row["project"], row["id"]) / "artifacts"
        progress = folder / "progress.json"
        if row["status"] == "running" and self.relay:
            progress = folder.parent / "remote-progress.json"
        try:
            counts = json.loads(progress.read_text()) if progress.is_file() else None
        except (OSError, ValueError):
            counts = None
        env = folder / "environment.json"
        try:
            actual = json.loads(env.read_text()) if row["status"] in {"completed", "failed"} and env.is_file() else {}
        except (OSError, ValueError):
            actual = {}
        data["actual_blender_version"] = actual.get("blender")
        if spec["operation"] == "probe":
            data["runtime"] = actual or None
            data["worker_resources"] = {
                "visible_cpu_count": len(os.sched_getaffinity(0)),
                "configured_job_memory_mib": self.tasks.sandbox.memory_mib,
                "configured_job_disk_mib": self.tasks.sandbox.disk_mib,
            }
            for name, path in (("cpu_quota", "cpu.max"), ("memory_limit_bytes", "memory.max")):
                try:
                    data["worker_resources"][name] = (Path("/sys/fs/cgroup") / path).read_text().strip()
                except OSError:
                    data["worker_resources"][name] = None
            if self.relay:
                try:
                    data["worker_resources"] = json.loads((folder / "worker-metrics.json").read_text())
                except (OSError, ValueError):
                    data["worker_resources"] = None
        data["frames_done"] = counts["done"] if counts else 0
        data["frames_total"] = len(spec.get("frames", []))
        if (folder / "execution.log").is_file() and row["status"] not in {"running", "queued"}:
            data["log_url"] = self.url("download", project=row["project"], task=row["id"], name="execution.log")
        return data

    def submit(self, project, request, operation, timeout=180, **params):
        p = self.store.require_project(project)["id"]
        if self.v1_running():
            raise ValueError("A legacy preview is running; submit after it finishes")
        spec = {"operation": operation, "timeout": timeout, "blender_series": self.supported_series, **params}
        series = spec.get("blender_series")
        if series != self.supported_series:
            raise ValueError(f"This worker is configured for Blender {self.supported_series}")
        return self.task_data(self.tasks.enqueue(p, request, spec))

    def output(self, project, task, name):
        p = self.store.require_project(project)["id"]
        row = self.store.task(p, str(task).replace("-", ""))
        allowed = [item["name"] for item in json.loads(row["outputs"] or "[]")]
        if row["status"] not in {"queued", "running"}:
            allowed.append("execution.log")
        if name not in allowed:
            raise ValueError("Requested output was not found")
        file = self.store.job_dir(p, row["id"]) / "artifacts" / name
        if not file.is_file() or file.is_symlink():
            raise ValueError("Requested output is unavailable")
        return file, row

    async def transfer(self, request: Request):
        try:
            data = self.store.authorize(request.path_params["token"], "upload")
        except (ValueError, KeyError):
            return JSONResponse({"error": "Invalid or expired upload authorization"}, status_code=403, headers=HEADER)
        if request.method == "GET":
            nonce = secrets.token_urlsafe(18)
            page = """<!doctype html><meta charset="utf-8"><title>Blender 资产上传</title>
            <h1>Blender 资产上传</h1><p>选择文件：""" + html.escape(data["name"]) + """</p>
            <form><input type="file" required><button>上传</button></form><pre aria-live="polite"></pre>
            <script nonce="""" + nonce + """">
            document.querySelector('form').onsubmit=async event=>{
              event.preventDefault(); const button=document.querySelector('button');
              const output=document.querySelector('pre'); button.disabled=true;
              try {
                const file=document.querySelector('input').files[0]; output.textContent='正在上传…';
                const result=await fetch(location.href,{method:'PUT',body:file});
                output.textContent=JSON.stringify(await result.json(),null,2);
              } catch(e) {output.textContent='上传失败，请重试。'} finally {button.disabled=false;}
            };
            </script>"""
            return HTMLResponse(page, headers={**HEADER, "Content-Security-Policy": (
                f"default-src 'none'; script-src 'nonce-{nonce}'; connect-src 'self'; frame-ancestors 'none'; form-action 'none'"
            )})
        directory = self.store.project_dir(data["project"])
        limit = self.upload_mib * MIB
        size, digest = 0, hashlib.sha256()
        tmp = None
        try:
            if int(request.headers.get("content-length", 0)) > limit:
                return JSONResponse({"error": "File too large"}, status_code=413, headers=HEADER)
            with tempfile.NamedTemporaryFile(dir=directory, delete=False) as pending:
                tmp = Path(pending.name)
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > limit:
                        return JSONResponse({"error": "File too large"}, status_code=413, headers=HEADER)
                    digest.update(chunk)
                    pending.write(chunk)
            if not kind_is_valid(tmp, data["name"]):
                return JSONResponse({"error": "File content does not match its declared type"}, status_code=415, headers=HEADER)
            asset = self.store.add_asset(data["project"], data["name"], tmp, digest.hexdigest(), size)
        except (OSError, ValueError):
            return JSONResponse({"error": "Upload could not be saved"}, status_code=400, headers=HEADER)
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
        return JSONResponse({"asset_id": asset["id"], "filename": asset["name"], "sha256": asset["hash"], "bytes": size},
                            status_code=201, headers=HEADER)

    async def download(self, request):
        try:
            data = self.store.authorize(request.path_params["token"], "download")
            if data.get("asset"):
                asset = self.store.asset(data["project"], data["asset"])
                path = self.store.asset_file(asset)
                name = Path(asset["name"]).name
            else:
                path, _ = self.output(data["project"], data["task"], data["name"])
                name = data["name"]
            if not path.is_file() or path.is_symlink():
                raise ValueError("Missing file")
        except (ValueError, KeyError):
            return JSONResponse({"error": "Invalid or expired download authorization"}, status_code=403, headers=HEADER)
        return FileResponse(path, filename=name, headers=HEADER)

    def register(self, mcp):
        store = self.store
        write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
        read = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

        @mcp.tool(annotations=write)
        def create_project(name: Name, request_id: UUID) -> dict[str, Any]:
            """Create a private production project. Reuse the request UUID only for retries."""
            p = store.create_project(name, str(request_id))
            return {"project_id": p["id"], "name": p["name"], "service_version": VERSION}

        @mcp.tool(annotations=read)
        def list_projects() -> dict[str, Any]:
            """List production projects this MCP credential can access."""
            return {"projects": store.all("SELECT id AS project_id,name,created FROM projects WHERE owner=? ORDER BY created", (store.owner,))}

        @mcp.tool(annotations=write)
        def upload_asset(project_id: UUID, filename: Name) -> dict[str, Any]:
            """Issue a 15-minute private upload URL. Open it to upload, or send raw bytes via PUT."""
            p = store.require_project(project_id.hex)
            name = project_path(filename)
            return {"upload_url": self.url("upload", project=p["id"], name=name),
                    "expires_in_seconds": 900, "max_bytes": self.upload_mib * MIB, "filename": name}

        @mcp.tool(annotations=read)
        def list_assets(project_id: UUID, limit: int = 50, offset: int = 0) -> dict[str, Any]:
            """List immutable source assets and SHA-256 versions in a production project."""
            p = store.require_project(project_id.hex)
            return {"assets": store.all(
                "SELECT id AS asset_id,name,hash AS sha256,size AS bytes,created FROM assets WHERE project=? ORDER BY created LIMIT ? OFFSET ?",
                (p["id"], max(1, min(limit, 100)), max(0, offset)),
            )}

        @mcp.tool(annotations=read)
        def get_asset(project_id: UUID, asset_id: UUID) -> dict[str, Any]:
            """Get a short-lived download link for an original source asset."""
            p = store.require_project(project_id.hex)
            asset = store.asset(p["id"], asset_id.hex)
            return {"asset_id": asset["id"], "filename": asset["name"], "sha256": asset["hash"],
                    "url": self.url("download", project=p["id"], asset=asset["id"])}

        @mcp.tool(annotations=write)
        def blender_info(project_id: UUID, request_id: UUID, test_engine: Engine | None = None) -> dict[str, Any]:
            """Run Blender to measure its actual version; optionally smoke-render a specific engine on CPU."""
            config = RenderSettings(engine=test_engine, width=128, height=128, samples=4).model_dump() if test_engine else None
            return self.submit(project_id.hex, str(request_id), "probe", render=config, frames=[1] if test_engine else [])

        @mcp.tool(annotations=write)
        def import_model(project_id: UUID, asset_id: UUID, request_id: UUID, dependency_asset_ids: list[UUID] | None = None) -> dict[str, Any]:
            """Import an uploaded model. Returns a versioned blend copy and a structure report."""
            p = store.require_project(project_id.hex)["id"]
            model = store.asset(p, asset_id.hex)
            suffix = Path(model["name"]).suffix.lower()
            if suffix not in MODELS:
                raise ValueError("A model asset is required")
            deps = list(dict.fromkeys(x.hex for x in dependency_asset_ids or []))
            if len(deps) > 16 or sum(store.asset(p, id)["size"] for id in [asset_id.hex, *deps]) > 64 * MIB:
                raise ValueError("Model and dependency inputs exceed the job limits")
            if any(Path(store.asset(p, aid)["name"]).suffix.lower() not in DEPENDENCIES for aid in deps):
                raise ValueError("Only texture and model dependency files may be added")
            return self.submit(p, str(request_id), "import", model="assets/" + model["name"],
                               asset_ids=[asset_id.hex, *deps], cad=suffix in {".step", ".stp"})

        @mcp.tool(annotations=write)
        def inspect_model(project_id: UUID, project_version: UUID, request_id: UUID) -> dict[str, Any]:
            """Inspect geometry bounds, local axes, pivots, hierarchy, materials and possible broad-phase overlaps."""
            store.require_project(project_id.hex)
            store.version(project_id.hex, project_version.hex)
            return self.submit(project_id.hex, str(request_id), "inspect", base=project_version.hex)

        @mcp.tool(annotations=write)
        def run_script(
            project_id: UUID, base_project_version: UUID, request_id: UUID,
            script_id: Literal["product_showcase", "edit_objects"],
            parameters: Showcase | None = None, edits: list[Edit] | None = None,
            blender_version: Literal["5.3"] = "5.3",
        ) -> dict[str, Any]:
            """Run an approved versioned recipe. Use edit_objects for targeted animation, groups, materials, cameras or lights."""
            p = store.require_project(project_id.hex)["id"]
            store.version(p, base_project_version.hex)
            asset_ids = []
            actions = [edit.model_dump() for edit in edits or []]
            if script_id == "product_showcase":
                if edits:
                    raise ValueError("Supply parameters for product_showcase")
            elif not 1 <= len(actions) <= 96 or parameters is not None:
                raise ValueError("Supply 1–96 structured edits for edit_objects")
            for edit in actions:
                texture = edit.pop("texture_asset_id", None)
                if texture:
                    asset = store.asset(p, UUID(texture).hex)
                    if Path(asset["name"]).suffix.lower() not in {".png", ".jpg", ".jpeg", ".hdr", ".exr"}:
                        raise ValueError("Texture edits require an image asset")
                    edit["texture"] = "assets/" + asset["name"]
                    if asset["id"] not in asset_ids:
                        asset_ids.append(asset["id"])
            if sum(store.asset(p, aid)["size"] for aid in asset_ids) > 64 * MIB:
                raise ValueError("Texture inputs exceed the job limits")
            recipe_hash = hashlib.sha256(self.tasks.sandbox.script.read_bytes()).hexdigest()
            return self.submit(
                p, str(request_id), script_id, base=base_project_version.hex, asset_ids=asset_ids,
                recipe=script_id + "@" + RECIPE_VERSION, recipe_sha256=recipe_hash,
                parameters=(parameters or Showcase()).model_dump() if script_id == "product_showcase" else None,
                actions=actions, blender_series=blender_version,
            )

        @mcp.tool(annotations=read)
        def open_project(project_id: UUID, project_version: UUID | None = None) -> dict[str, Any]:
            """List saved immutable versions or return the selected Blender project file."""
            p = store.require_project(project_id.hex)["id"]
            if project_version is None:
                return {"versions": store.all("SELECT * FROM versions WHERE project=? ORDER BY created DESC LIMIT 100", (p,))}
            item = store.version(p, project_version.hex)
            return {**item, "blend_url": self.url("download", project=p, task=item["task"], name="scene.blend")}

        def render_task(project, version, request, operation, frames, config, timeout):
            p = store.require_project(project.hex)["id"]
            store.version(p, version.hex)
            if config.device == "GPU":
                raise ValueError("GPU is not configured")
            if config.mode == "preview" and (max(config.width, config.height) > 1280 or min(config.width, config.height) > 720):
                raise ValueError("Preview mode supports up to 720p")
            if config.mode == "sample" and (config.engine == "workbench" or max(config.width, config.height) > 1920 or min(config.width, config.height) > 1080):
                raise ValueError("Sample mode supports Eevee or Cycles up to 1080p")
            if config.mode == "production" and config.engine == "workbench":
                raise ValueError("Production mode requires Eevee or Cycles")
            return self.submit(p, str(request), operation, timeout=timeout, base=version.hex,
                               render=config.model_dump(), frames=frames)

        @mcp.tool(annotations=write)
        def render_image(project_id: UUID, project_version: UUID, request_id: UUID, frame: Frame,
                         settings: RenderSettings | None = None, time_limit: int = 180) -> dict[str, Any]:
            """Render one frame from a saved project version, optionally using a named camera."""
            return render_task(project_id, project_version, request_id, "render_image", [frame],
                               settings or RenderSettings(), max(10, min(time_limit, 1200)))

        @mcp.tool(annotations=write)
        def render_animation(project_id: UUID, project_version: UUID, request_id: UUID, start: Frame, end: Frame,
                             settings: RenderSettings | None = None, time_limit: int = 240) -> dict[str, Any]:
            """Render an inclusive range to numbered PNGs and H.264 MP4. Split long shots into bounded segments."""
            if not 1 <= end - start + 1 <= 240:
                raise ValueError("Submit a range of 1–240 frames")
            return render_task(project_id, project_version, request_id, "render_animation", list(range(start, end + 1)),
                               settings or RenderSettings(), max(10, min(time_limit, 1200)))

        @mcp.tool(annotations=write)
        def extract_frames(project_id: UUID, project_version: UUID, request_id: UUID, frames: list[Frame],
                           settings: RenderSettings | None = None) -> dict[str, Any]:
            """Render 1–12 selected review frames without rerendering the entire shot."""
            if not 1 <= len(frames) <= 12:
                raise ValueError("Supply 1–12 frame numbers")
            return render_task(project_id, project_version, request_id, "extract_frames", sorted(set(frames)),
                               settings or RenderSettings(), 240)

        @mcp.tool(annotations=read)
        def task_status(project_id: UUID, task_id: UUID) -> dict[str, Any]:
            """Query persisted status, errors, logs, frames completed and the output index."""
            p = store.require_project(project_id.hex)["id"]
            if self.relay:
                self.relay.reap()
            return self.task_data(store.task(p, task_id.hex))

        @mcp.tool(annotations=write)
        def cancel_task(project_id: UUID, task_id: UUID) -> dict[str, Any]:
            """Request cancellation. Status becomes cancelled after the process ends."""
            p = store.require_project(project_id.hex)["id"]
            requested = self.tasks.cancel(store.task(p, task_id.hex))
            return {"cancellation_requested": requested, **self.task_data(store.task(p, task_id.hex))}

        @mcp.tool(annotations=write)
        def retry_task(project_id: UUID, task_id: UUID, request_id: UUID) -> dict[str, Any]:
            """Retry a failed, interrupted or cancelled task from its original version."""
            p = store.require_project(project_id.hex)["id"]
            row = store.task(p, task_id.hex)
            if row["status"] not in {"failed", "interrupted", "cancelled"}:
                raise ValueError("Only unsuccessful tasks can be retried")
            spec = json.loads(row["spec"])
            return self.task_data(self.tasks.enqueue(p, str(request_id), spec))

        @mcp.tool(annotations=read)
        def get_output(project_id: UUID, task_id: UUID, filename: Name) -> dict[str, Any]:
            """Issue a short-lived signed download link for a listed artifact or execution.log."""
            file, row = self.output(project_id.hex, task_id.hex, filename)
            return {"filename": file.name, "bytes": file.stat().st_size,
                    "url": self.url("download", project=row["project"], task=row["id"], name=file.name)}

        @mcp.tool(annotations=read)
        def review_image(project_id: UUID, task_id: UUID, filename: Name) -> Image:
            """Read an already-rendered PNG as actual image content for GPT visual review."""
            file, _ = self.output(project_id.hex, task_id.hex, filename)
            if file.suffix != ".png" or file.stat().st_size > 5 * MIB:
                raise ValueError("Choose a PNG review image at most 5 MiB")
            return Image(path=str(file), format="png")

        @mcp.tool(annotations=write)
        def export_project(project_id: UUID, request_id: UUID) -> dict[str, Any]:
            """Create a private backup ZIP, including project catalog, original assets and saved versions."""
            return self.submit(project_id.hex, str(request_id), "export")
