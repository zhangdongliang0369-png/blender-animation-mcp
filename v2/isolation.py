"""Run a single production command with fresh namespaces and isolated files."""
import asyncio
import contextlib
import os
from pathlib import Path
import shutil
import signal
import sys
import time

MIB = 1024 * 1024


class WorkerError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class Sandbox:
    def __init__(self):
        self.blender = Path(os.environ.get("BLENDER_V2_BINARY") or shutil.which("blender") or "/usr/bin/blender").resolve()
        self.script = Path(__file__).with_name("worker.py").resolve()
        self.memory_mib = int(os.environ.get("BLENDER_V2_MEMORY_MIB", "1024"))
        self.disk_mib = int(os.environ.get("BLENDER_V2_JOB_DISK_MIB", "192"))
        self.peak_rss = 0

    def command(self, job: Path, args: list[str], seconds: int) -> list[str]:
        blender = str(self.blender)
        bundled = not self.blender.is_relative_to("/usr")
        mounts = []
        if bundled:
            mounts.extend(["--ro-bind", str(self.blender.parent), "/blender"])
            blender = "/blender/" + self.blender.name
        if args[0] == "@cad-python":
            executable = Path(sys.executable)
            for prefix in dict.fromkeys([Path(sys.base_prefix), executable.parent.parent]):
                if not prefix.is_relative_to("/usr"):
                    mounts.extend(["--ro-bind", str(prefix), str(prefix)])
            mounts.extend(["--ro-bind", str(self.script.with_name("cad_worker.py")), "/cad_worker.py"])
            args = [str(executable), *args[1:]]
        else:
            args = [blender if item == "@blender" else item for item in args]
        return [
            "/usr/bin/prlimit", f"--cpu={seconds}", "--as=8589934592",
            f"--fsize={self.disk_mib * MIB}", "--core=0", "--",
            "/usr/bin/bwrap", "--unshare-all", "--die-with-parent", "--cap-drop", "ALL",
            "--clearenv", "--setenv", "PATH", "/usr/bin", "--setenv", "HOME", "/tmp",
            "--setenv", "TMPDIR", "/tmp", "--setenv", "XDG_CACHE_HOME", "/tmp",
            "--setenv", "PYTHONNOUSERSITE", "1", "--setenv", "LIBGL_ALWAYS_SOFTWARE", "1",
            "--setenv", "LP_NUM_THREADS", "1", "--setenv", "OMP_NUM_THREADS", "1",
            "--setenv", "MALLOC_ARENA_MAX", "2",
            "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
            "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
            "--ro-bind-try", "/etc/ld.so.cache", "/etc/ld.so.cache",
            "--ro-bind-try", "/etc/fonts", "/etc/fonts",
            "--ro-bind-try", "/etc/alternatives", "/etc/alternatives",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", *mounts,
            "--ro-bind", str(job / "inputs"), "/inputs",
            "--bind", str(job / "artifacts"), "/output",
            "--ro-bind", str(self.script), "/worker.py", "--chdir", "/output", "--", *args,
        ]

    async def run(self, job: Path, args: list[str], seconds: int, log, cancelled=None):
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.command(job, args, seconds), stdout=log, stderr=asyncio.subprocess.STDOUT,
                env={"PATH": "/usr/bin"}, start_new_session=True,
            )
        except OSError as exc:
            raise WorkerError("SANDBOX_UNAVAILABLE", "Failed to launch isolated renderer") from exc
        wait = asyncio.create_task(proc.wait())
        deadline = time.monotonic() + seconds
        try:
            while not wait.done():
                await asyncio.wait({wait}, timeout=0.25)
                if cancelled and cancelled():
                    raise WorkerError("CANCELLED", "Task cancelled")
                if time.monotonic() > deadline:
                    raise WorkerError("TIMEOUT", "Task exceeded its execution time limit")
                allocated = sum(
                    path.stat().st_size for path in (job / "artifacts").rglob("*")
                    if path.is_file() and not path.is_symlink()
                )
                if allocated > self.disk_mib * MIB:
                    raise WorkerError("DISK_LIMIT", "Task exceeded its artifact disk limit")
                current_rss = process_group_rss(proc.pid)
                self.peak_rss = max(self.peak_rss, current_rss)
                if current_rss > self.memory_mib * MIB:
                    raise WorkerError("MEMORY_LIMIT", "Task exceeded its memory limit")
            if wait.result() != 0:
                log.flush()
                log.seek(max(0, log.tell() - 8192))
                details = log.read().decode(errors="replace").strip()
                log.seek(0, os.SEEK_END)
                code = "SANDBOX_UNAVAILABLE" if "bwrap:" in details else "WORKER_FAILED"
                raise WorkerError(code, details[-4000:] or f"Process exited with {wait.result()}")
        finally:
            if not wait.done():
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                await asyncio.shield(wait)


def process_group_rss(group: int) -> int:
    """Count only resident memory of the isolated job's process group."""
    page_size = os.sysconf("SC_PAGE_SIZE")
    pages = 0
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        with contextlib.suppress(OSError, ValueError, IndexError):
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[2]) == group:
                pages += int(fields[21])
    return pages * page_size
