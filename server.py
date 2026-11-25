import asyncio
import logging
import os
import time
from pathlib import Path
from shutil import rmtree
from typing import Literal, TypedDict
from uuid import UUID, uuid4

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import FileResponse, Response
from starlette.routing import Route

railway_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
origin = (
    os.environ.get("BLENDER_PUBLIC_URL")
    or os.environ.get("RENDER_EXTERNAL_URL")
    or (f"https://{railway_domain}" if railway_domain else None)
    or f"http://127.0.0.1:{os.environ.get('PORT', '10000')}"
).rstrip("/")
access_id = os.environ["BLENDER_ACCESS_ID"]
work = Path(os.environ.get("BLENDER_RENDER_DIR", "./renders")).resolve()
work.mkdir(mode=0o700, parents=True, exist_ok=True)
active_render: asyncio.Task[None] | None = None
logger = logging.getLogger(__name__)
mcp = FastMCP(
    "Blender 云端预演",
    instructions="提交基础图形动画预演。render_preview 返回任务编号。用 preview_status 查询结果；仍在渲染时把编号告诉用户，收到用户查询后再查看。仅发送图形和颜色。",
    streamable_http_path=f"/{access_id}/mcp",
    stateless_http=True,
    json_response=True,
    max_request_body_size=16384,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", origin.split("://", 1)[1]],
    ),
)

class RenderJob(TypedDict):
    job_id: str
    status: Literal["rendering", "completed", "failed"]
    video_url: str | None
    blend_url: str | None


async def render(folder: Path, shape: str, color: str) -> None:
    project = folder / "scene.blend"
    try:
        with (folder / "render.log").open("wb") as log:
            process = await asyncio.create_subprocess_exec(
                "blender", "-b", "--factory-startup", "-t", "2",
                "--python-exit-code", "1", "--python",
                str(Path(__file__).with_name("animate.py")), "--", shape, color,
                str(project), stdout=log, stderr=asyncio.subprocess.STDOUT,
            )
            try:
                status = await asyncio.wait_for(process.wait(), timeout=180)
            except (TimeoutError, asyncio.CancelledError):
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                raise
        if status or not (project.is_file() and (folder / "preview.mp4").is_file()):
            raise RuntimeError(f"Blender exited with status {status} or did not produce both files")
    except asyncio.CancelledError:
        (folder / "failed").touch()
        raise
    except Exception:
        logger.exception("Blender preview rendering failed")
        (folder / "failed").touch()
    else:
        (folder / "completed").touch()


@mcp.tool(annotations=ToolAnnotations(
    title="生成 Blender 动画预演", readOnlyHint=False,
    destructiveHint=False, idempotentHint=False, openWorldHint=False,
))
async def render_preview(
    shape: Literal["cube", "sphere", "torus"],
    color: Literal["blue", "red", "gold"],
) -> RenderJob:
    """提交 2 秒的 Blender 基础图形动画。用返回的任务编号查询结果。"""
    global active_render
    if active_render is not None and not active_render.done():
        raise RuntimeError("Blender 正在渲染，请稍后提交新任务。")
    for folder in work.iterdir():
        if folder.is_dir() and time.time() - folder.stat().st_mtime > 3600:
            rmtree(folder)
    job_id = str(uuid4())
    folder = work / job_id
    folder.mkdir(mode=0o700)
    active_render = asyncio.create_task(render(folder, shape, color))
    return {"job_id": job_id, "status": "rendering", "video_url": None, "blend_url": None}


@mcp.tool(annotations=ToolAnnotations(
    title="查看 Blender 渲染结果", readOnlyHint=True, openWorldHint=False,
))
async def preview_status(job_id: UUID) -> RenderJob:
    """查询任务状态。渲染成功后返回 MP4 与 Blender 工程下载链接。"""
    folder = work / str(job_id)
    if not folder.is_dir() or time.time() - folder.stat().st_mtime > 3600:
        raise ValueError("找不到任务，或任务已经过期。")
    if (folder / "completed").is_file():
        files_url = f"{origin}/files/{job_id}"
        return {
            "job_id": str(job_id), "status": "completed",
            "video_url": f"{files_url}/preview.mp4",
            "blend_url": f"{files_url}/scene.blend",
        }
    status = "failed" if (folder / "failed").is_file() else "rendering"
    return {"job_id": str(job_id), "status": status, "video_url": None, "blend_url": None}


async def download(request: Request):
    filename = request.path_params["filename"]
    if filename not in {"preview.mp4", "scene.blend"}:
        return Response(status_code=404)
    folder = work / str(request.path_params["job_id"])
    path = folder / filename
    if not (folder / "completed").is_file() or not path.is_file() or time.time() - folder.stat().st_mtime > 3600:
        return Response(status_code=404)
    if filename.endswith(".mp4"):
        return FileResponse(path, media_type="video/mp4")
    return FileResponse(path, filename=filename, media_type="application/octet-stream")

async def health(_request: Request):
    return Response("ok")

app = mcp.streamable_http_app()
app.routes.append(Route("/health", health))
app.routes.append(Route("/files/{job_id:uuid}/{filename}", download))
