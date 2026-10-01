from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Process:
    pid: int
    ppid: int
    pgid: int
    sid: int
    start_time: int
    state: str
    comm: str
    argv: tuple[str, ...]


def _process(pid: int, proc: Path) -> Process | None:
    try:
        stat = (proc / str(pid) / "stat").read_text(encoding="utf-8")
        tail = stat.rsplit(")", 1)[1].split()
        argv = tuple(
            item.decode(errors="replace") for item in (proc / str(pid) / "cmdline").read_bytes().split(b"\0") if item
        )
        comm = (proc / str(pid) / "comm").read_text(encoding="utf-8").strip()
    except (OSError, IndexError, ValueError):
        return None
    return Process(
        pid=pid,
        ppid=int(tail[1]),
        pgid=int(tail[2]),
        sid=int(tail[3]),
        start_time=int(tail[19]),
        state=tail[0],
        comm=comm,
        argv=argv,
    )


def processes(proc: Path = Path("/proc")) -> dict[int, Process]:
    try:
        pids = [int(entry.name) for entry in proc.iterdir() if entry.name.isdigit()]
    except OSError:
        return {}
    return {item.pid: item for pid in pids if (item := _process(pid, proc)) is not None}


def _target(process: Process) -> str:
    names = {process.comm, *(Path(value).name for value in process.argv[:2])}
    if "claude" in names or any(value.endswith("claude-code/cli.js") for value in process.argv[:2]):
        return "claude"
    if any(value == "codex" or value.startswith("codex-") for value in names):
        return "codex"
    return ""
