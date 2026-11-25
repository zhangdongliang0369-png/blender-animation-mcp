"""Live MCP acceptance test. Provide a private real Blender fixture by path."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
from uuid import uuid4

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

origin = os.environ["V2_TEST_ORIGIN"].rstrip("/")
fixture = Path(os.environ["V2_TEST_MODEL"])
report_dir = Path(os.environ["V2_TEST_OUTPUT"])
report_dir.mkdir(parents=True, exist_ok=True)


async def main():
    async with httpx.AsyncClient(timeout=15) as http:
        async with streamable_http_client(f"{origin}/{os.environ['V2_TEST_ACCESS']}/mcp") as (rd, wr, _):
            async with ClientSession(rd, wr) as session:
                await session.initialize()
                tools = {tool.name for tool in (await session.list_tools()).tools}
                assert tools.issuperset({"render_preview", "preview_status", "run_script", "render_image", "export_project"})

                async def call(tool, **args):
                    response = await session.call_tool(tool, args)
                    assert not response.isError, (tool, response.content)
                    return response.structuredContent

                async def reject(tool, **args):
                    response = await session.call_tool(tool, args)
                    assert response.isError, tool

                async def complete(row, target="completed"):
                    assert row["status"] in {"queued", "running", target}
                    for _ in range(130):
                        if row["status"] not in {"queued", "running"}:
                            break
                        await asyncio.sleep(1)
                        row = await call("task_status", project_id=row["project_id"], task_id=row["task_id"])
                    print("task", row["operation"], row["status"], row.get("actual_blender_version"), flush=True)
                    assert row["status"] == target, row
                    return row

                async def artifact(row, name, local_name=None):
                    item = await call("get_output", project_id=row["project_id"], task_id=row["task_id"], filename=name)
                    response = await http.get(item["url"])
                    assert response.status_code == 200 and len(response.content) == item["bytes"]
                    if local_name:
                        (report_dir / local_name).write_bytes(response.content)
                    return response.content

                request_id = str(uuid4())
                project = (await call("create_project", name="Nomo model acceptance", request_id=request_id))["project_id"]
                assert project == (await call("create_project", name="Nomo model acceptance", request_id=request_id))["project_id"]
                other = (await call("create_project", name="Authorization boundary", request_id=str(uuid4())))["project_id"]
                await reject("upload_asset", project_id=project, filename="../escape.blend")
                link = (await call("upload_asset", project_id=project, filename=fixture.name))["upload_url"]
                assert (await http.get(link)).status_code == 200
                assert (await http.put(link + "x", content=b"invalid")).status_code == 403
                assert (await http.put(link, content=b"not a blend file")).status_code == 415
                source = fixture.read_bytes()
                uploaded = await http.put(link, content=source)
                assert uploaded.status_code == 201, uploaded.text
                asset = uploaded.json()
                assert asset["sha256"] == hashlib.sha256(source).hexdigest()
                assert (await http.put(link, content=source)).json()["asset_id"] == asset["asset_id"]
                await reject("import_model", project_id=other, asset_id=asset["asset_id"], request_id=str(uuid4()))
                imported = await complete(await call("import_model", project_id=project,
                    asset_id=asset["asset_id"], request_id=str(uuid4())))
                original_version = imported["project_version"]
                original_copy = await artifact(imported, "scene.blend")
                scan = json.loads(await artifact(imported, "model-report.json", "model-report.json"))
                assert scan["mesh_count"] > 0 and not scan["missing_dependencies"], scan.get("missing_dependencies")
                await reject("open_project", project_id=other, project_version=original_version)
                await reject("get_output", project_id=other, task_id=imported["task_id"], filename="scene.blend")
                req = str(uuid4())
                showcase_args = dict(project_id=project, base_project_version=original_version, request_id=req,
                                     script_id="product_showcase", parameters={"start": 1, "end": 48})
                pending = await call("run_script", **showcase_args)
                assert pending["task_id"] == (await call("run_script", **showcase_args))["task_id"]
                showcase = await complete(pending)
                await artifact(showcase, "scene.blend", "nomo-showcase.blend")
                await artifact(showcase, "execution.log", "nomo-showcase.log")
                version = showcase["project_version"]
                settings = {"width": 854, "height": 480, "fps": 24, "engine": "workbench"}
                render = await complete(await call("render_animation", project_id=project, project_version=version,
                    request_id=str(uuid4()), settings=settings, start=1, end=48, time_limit=180))
                assert render["frames_done"] == render["frames_total"] == 48
                await artifact(render, "preview.mp4", "nomo-preview.mp4")
                await artifact(render, "seq-000001.png", "nomo-keyframe.png")
                ffprobe = subprocess.check_output(
                    ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json",
                     str(report_dir / "nomo-preview.mp4")], text=True
                )
                video = json.loads(ffprobe)
                assert video["streams"][0]["codec_name"] == "h264" and float(video["format"]["duration"]) == 2
                review = await session.call_tool("review_image", {
                    "project_id": project, "task_id": render["task_id"], "filename": "seq-000001.png"})
                assert not review.isError and any(item.type == "image" for item in review.content)
                modified = await complete(await call("run_script", project_id=project,
                    base_project_version=version, request_id=str(uuid4()), script_id="edit_objects",
                    edits=[{"kind": "material", "object_name": scan["objects"][next(
                        i for i, obj in enumerate(scan["objects"]) if obj["type"] == "MESH")]["name"], "rgb": [0.07, 0.25, 0.5]}]))
                revised = await complete(await call("extract_frames", project_id=project, project_version=modified["project_version"],
                    request_id=str(uuid4()), frames=[12], settings=settings))
                await artifact(revised, "frame-12.png", "nomo-modified-frame.png")
                assert await artifact(imported, "scene.blend") == original_copy
                failed = await complete(await call("run_script", project_id=project, base_project_version=version,
                    request_id=str(uuid4()), script_id="edit_objects",
                    edits=[{"kind": "transform", "object_name": "NO_SUCH_OBJECT", "location": [1, 0, 0]}]), "failed")
                assert "NO_SUCH_OBJECT" in failed["error"] and not failed["outputs"]
                assert (await http.get(failed["log_url"])).status_code == 200
                backup = await complete(await call("export_project", project_id=project, request_id=str(uuid4())))
                assert (await artifact(backup, "project.zip")).startswith(b"PK")
                result = {
                    "project_id": project, "source_asset": asset, "original_version": original_version,
                    "showcase_version": version, "revised_version": modified["project_version"],
                    "job_ids": {"model": imported["task_id"], "showcase": showcase["task_id"], "animation": render["task_id"]},
                    "video": {"codec": video["streams"][0]["codec_name"], "duration": video["format"]["duration"],
                              "frames": render["frames_done"], "width": video["streams"][0]["width"],
                              "height": video["streams"][0]["height"]},
                }
                (report_dir / "acceptance.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps(result, ensure_ascii=False), flush=True)

if __name__ == "__main__":
    asyncio.run(main())
