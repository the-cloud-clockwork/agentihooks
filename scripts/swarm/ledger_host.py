"""What the swarm learns about the ledger server without asking it: its CPU, its age and the newest commit on dev."""

import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PROC = Path("/proc")
SAMPLE_S = 0.5
GIT_TIMEOUT_S = 5


def folder(environ: dict) -> Path:
    return Path(environ.get("LEDGER_DIR") or Path.home() / "development-ledger").expanduser()


def server_pid(ledger_dir: Path) -> int | None:
    try:
        return int((ledger_dir / ".server.pid").read_text())
    except (OSError, ValueError):
        return None


def _stat(pid: int, proc: Path) -> tuple[int, int]:
    tail = (proc / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
    return int(tail[11]) + int(tail[12]), int(tail[19])


def server(
    pid: int | None,
    proc: Path = PROC,
    sleep: Callable[[float], None] = time.sleep,
    hertz: int = os.sysconf("SC_CLK_TCK"),
) -> dict:
    if pid is None:
        return {"cpu": None, "started_minutes": None}
    try:
        before, start = _stat(pid, proc)
        sleep(SAMPLE_S)
        after, _ = _stat(pid, proc)
        uptime = float((proc / "uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return {"cpu": None, "started_minutes": None}
    return {
        "cpu": round((after - before) / hertz / SAMPLE_S * 100),
        "started_minutes": int((uptime - start / hertz) // 60),
    }


def newest_on_dev(repo: Path = REPO, now: float | None = None) -> dict:
    try:
        found = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%ct%x09%s", "origin/dev"],
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"newest": None, "merged_minutes": None}
    stamp, _, subject = found.stdout.strip().partition("\t")
    if found.returncode or not stamp.isdigit():
        return {"newest": None, "merged_minutes": None}
    return {"newest": subject, "merged_minutes": int(((now or time.time()) - int(stamp)) // 60)}


def facts() -> dict:
    return {**server(server_pid(folder(os.environ))), **newest_on_dev()}
