import json
import os
import threading
from pathlib import Path


def process(pid: int, proc: Path) -> dict | None:
    try:
        root = proc / str(pid)
        stat = (root / "stat").read_text()
        tail = stat.rsplit(")", 1)[1].split()
        return {
            "pid": pid,
            "ppid": int(tail[1]),
            "start": int(tail[19]),
            "state": tail[0],
            "comm": stat.split("(", 1)[1].rsplit(")", 1)[0],
            "argv": (root / "cmdline").read_bytes().split(b"\0"),
        }
    except (OSError, ValueError, IndexError):
        return None


def shared(folder: Path, port: int) -> bool:
    return folder.resolve() == (Path.home() / "development-ledger").resolve() and port == 8765


def owner(environ: dict, proc: Path = Path("/proc")) -> tuple[int, int] | None:
    if "LEDGER_RUN_PID" in environ:
        pid = int(environ["LEDGER_RUN_PID"])
        parent = process(pid, proc)
        start = environ.get("LEDGER_RUN_START")
        return pid, int(start) if start is not None else parent["start"] if parent else 0
    parent = process(os.getppid(), proc)
    fallback = parent
    while parent and parent["pid"] > 1:
        if any(Path(arg.decode()).name in {"pytest", "mutmut"} for arg in parent["argv"]):
            return parent["pid"], parent["start"]
        if parent["comm"] in {"bash", "sh", "zsh", "fish", "codex", "claude"}:
            fallback = parent
            break
        parent = process(parent["ppid"], proc)
    return (fallback["pid"], fallback["start"]) if fallback and fallback["pid"] > 1 else None


def environment(folder: Path, port: int) -> dict:
    environ = dict(os.environ)
    if shared(folder, port):
        environ.pop("LEDGER_RUN_PID", None)
        environ.pop("LEDGER_RUN_START", None)
    elif identity := owner(environ):
        environ["LEDGER_RUN_PID"], environ["LEDGER_RUN_START"] = map(str, identity)
    return environ


def ended(identity: tuple[int, int], proc: Path = Path("/proc")) -> bool:
    parent = process(identity[0], proc)
    return parent is None or parent["start"] != identity[1] or parent["state"] == "Z"


def watch(server, folder: Path, port: int) -> threading.Event:
    stopped = threading.Event()
    if shared(folder, port):
        return stopped
    identity = owner(environment(folder, port))
    if identity is None:
        return stopped
    os.environ["LEDGER_RUN_PID"], os.environ["LEDGER_RUN_START"] = map(str, identity)
    cwd = Path.cwd()
    (folder / ".server.owner.json").write_text(json.dumps({"pid": identity[0], "start": identity[1]}))

    def until_ended():
        while not ended(identity) and folder.exists() and cwd.exists():
            if stopped.wait(0.25):
                return
        server.shutdown()

    threading.Thread(target=until_ended, daemon=True).start()
    return stopped
