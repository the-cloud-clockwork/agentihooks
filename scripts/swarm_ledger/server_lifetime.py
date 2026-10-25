import json
import os
import threading
from pathlib import Path

from scripts.swarm_ledger import ledger_link


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
            "argv": (root / "cmdline").read_bytes().split(b"\0"),
        }
    except (OSError, ValueError, IndexError):
        return None


def shared(folder: Path, port: int) -> bool:
    return (
        ledger_link.shared_directory(environ={"LEDGER_DIR": str(folder)}) and port == ledger_link.address(environ={})[1]
    )


def owner(environ: dict, proc: Path = Path("/proc")) -> tuple[int, int] | None:
    if "LEDGER_RUN_PID" in environ:
        pid = int(environ["LEDGER_RUN_PID"])
        parent = process(pid, proc)
        start = environ.get("LEDGER_RUN_START")
        return pid, int(start) if start is not None else parent["start"] if parent else 0
    parent = process(os.getppid(), proc)
    while parent and parent["pid"] > 1:
        argv = [arg.decode() for arg in parent["argv"]]
        helper = any(
            Path(arg).name in {"agentihooks", "ledger.py", "ledger_hook.py", "ledger_server.py"} for arg in argv[:2]
        )
        if not helper and argv[1:3] not in [["-m", "hooks"], ["-m", "scripts.install"]]:
            return parent["pid"], parent["start"]
        parent = process(parent["ppid"], proc)
    return None


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
    return parent is None or parent["start"] != identity[1] or parent["state"] in {"Z", "X"}


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
