"""Run the isolated Blender worker using only outbound HTTPS to the relay."""
import asyncio
from contextlib import suppress
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time

import httpx

from .isolation import Sandbox, WorkerError
from .relay import ID, INPUT_MIB, MIB, unpack_package, write_package
from .tasks import run_worker


async def validate_node(sandbox, root):
    with tempfile.TemporaryDirectory(dir=root) as tmp:
        folder = Path(tmp)
        (folder / "inputs").mkdir()
        (folder / "artifacts").mkdir()
        sentinel = folder / "outside"
        sentinel.write_text("secret outside the worker mounts")
        (folder / "inputs" / "sentinel").write_text("read-only")
        code = f"""
import os, resource, socket
from pathlib import Path
assert not Path({str(sentinel)!r}).exists()
assert not any(key.startswith('BLENDER_') for key in os.environ)
assert os.readlink('/proc/self/ns/net') != {os.readlink('/proc/self/ns/net')!r}
assert Path('/inputs/sentinel').read_text() == 'read-only'
try:
    Path('/inputs/sentinel').write_text('modified')
except OSError:
    pass
else:
    raise RuntimeError('Input mount is writable')
try:
    socket.create_connection(('198.51.100.1', 443), timeout=1)
except OSError:
    pass
else:
    raise RuntimeError('Outgoing network is available')
for limit in (resource.RLIMIT_CPU, resource.RLIMIT_AS, resource.RLIMIT_FSIZE):
    assert 0 < resource.getrlimit(limit)[0] < 10**11
"""
        with (folder / "artifacts" / "preflight.log").open("w+b") as log:
            await sandbox.run(folder, ["/usr/bin/python3", "-c", code], 10, log)
            await sandbox.run(folder, ["@blender", "--version"], 15, log)
            log.seek(0)
            version = log.readline().decode().strip()
            if not version.startswith("Blender 5.3."):
                raise RuntimeError("Expected Blender 5.3 runtime")
        print(f"Worker runtime verified: {version}, private network, read-only inputs and resource ceilings", flush=True)


async def send_file(path):
    with path.open("rb") as source:
        while block := source.read(256 * 1024):
            yield block


async def work_one(client, sandbox, root, recipe):
    with tempfile.TemporaryDirectory(dir=root) as tmp:
        folder = Path(tmp)
        inputs, outputs = folder / "inputs", folder / "artifacts"
        incoming = folder / "in.zip"
        async with client.stream("POST", "/v2/worker/claim", headers={"X-Worker-Recipe": recipe}) as reply:
            if reply.status_code == 204:
                return False
            reply.raise_for_status()
            job = reply.headers.get("x-worker-job", "")
            lease = reply.headers.get("x-worker-lease", "")
            if not ID.fullmatch(job) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", lease):
                raise ValueError("Invalid job or lease")
            size = 0
            with incoming.open("wb") as source:
                async for chunk in reply.aiter_bytes():
                    size += len(chunk)
                    if size > INPUT_MIB * MIB:
                        raise ValueError("Worker package exceeds the transfer limit")
                    source.write(chunk)
        inputs.mkdir(mode=0o700)
        outputs.mkdir(mode=0o700)
        unpack_package(incoming, inputs, INPUT_MIB * MIB, output=False)
        spec = json.loads((inputs / "job.json").read_text())
        header = {"X-Worker-Lease": lease}
        remote = f"/v2/worker/{job}"
        deadline = time.monotonic() + min(1200, int(spec["timeout"]))
        renewed = time.monotonic()
        cancelled = False

        async def heartbeat():
            nonlocal renewed, cancelled
            while not cancelled:
                try:
                    progress = {}
                    with suppress(OSError, ValueError):
                        progress = json.loads((outputs / "progress.json").read_text())
                    response = await client.post(remote + "/heartbeat", headers={
                        **header, "X-Worker-Frames-Done": str(progress.get("done", "")),
                    }, timeout=6)
                    if response.status_code in {401, 409}:
                        cancelled = True
                        return
                    response.raise_for_status()
                    renewed = time.monotonic()
                    cancelled = bool(response.json()["cancel"])
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(2)

        def stopped():
            return cancelled or time.monotonic() - renewed >= 25

        def remaining():
            seconds = int(deadline - time.monotonic())
            if stopped():
                raise WorkerError("CANCELLED", "Task authorization ended")
            if seconds <= 0:
                raise WorkerError("TIMEOUT", "Task exceeded its execution time limit")
            return seconds

        heartbeat_task = asyncio.create_task(heartbeat())
        status, error = "completed", "WORKER_OK"
        sandbox.peak_rss = 0
        start = time.monotonic()
        try:
            with (outputs / "execution.log").open("w+b") as log:
                try:
                    await run_worker(sandbox, folder, spec, log, remaining, stopped)
                except WorkerError as exc:
                    status = "cancelled" if exc.code == "CANCELLED" else "failed"
                    error = exc.code
                    log.write(("\n" + str(exc)[:4000] + "\n").encode())
            metrics = {
                "elapsed_seconds": round(time.monotonic() - start, 2),
                "peak_process_group_rss_bytes": sandbox.peak_rss,
                "visible_cpu_count": len(os.sched_getaffinity(0)),
                "configured_job_memory_mib": sandbox.memory_mib,
                "configured_job_disk_mib": sandbox.disk_mib,
            }
            for name, filename in (("cpu_quota", "cpu.max"), ("memory_limit_bytes", "memory.max")):
                with suppress(OSError):
                    metrics[name] = (Path("/sys/fs/cgroup") / filename).read_text().strip()
            (outputs / "worker-metrics.json").write_text(json.dumps(metrics))
            package = folder / "result.zip"
            write_package(outputs, package)
            response = await client.put(remote + "/finish", content=send_file(package), headers={
                **header, "X-Worker-Status": status, "X-Worker-Error": error,
                "Content-Type": "application/zip", "Content-Length": str(package.stat().st_size),
            }, timeout=35)
            if response.status_code != 409:
                response.raise_for_status()
            print(f"{job}: {status}; control response {response.status_code}", flush=True)
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
        return True


async def main():
    root = Path(os.environ["BLENDER_REMOTE_WORKSPACE"]).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    endpoint = httpx.URL(os.environ["BLENDER_REMOTE_CONTROL"])
    if endpoint.scheme != "https" and endpoint.host not in {"localhost", "127.0.0.1"}:
        raise ValueError("Remote workers require HTTPS")
    sandbox = Sandbox()
    await validate_node(sandbox, root)
    recipe = hashlib.sha256(sandbox.script.read_bytes()).hexdigest()
    async with httpx.AsyncClient(base_url=endpoint, timeout=20, headers={
        "Authorization": "Bearer " + os.environ["BLENDER_V2_WORKER_KEY"],
    }) as client:
        while True:
            try:
                if await work_one(client, sandbox, root, recipe):
                    continue
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {401, 403, 409}:
                    raise
                print("Worker request declined:", exc.response.status_code, flush=True)
            except (httpx.TransportError, ValueError) as exc:
                print("Worker request interrupted:", type(exc).__name__, flush=True)
            await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(main())
