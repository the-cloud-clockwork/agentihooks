import json
import os
import signal
import time
from contextlib import suppress
from pathlib import Path

from hooks.proc import Process, _process
from scripts.swarm_ledger.ledger_link import address
from scripts.swarm_ledger.server_lifetime import shared


def server_process(row: Process) -> bool:
    return "--serve" in row.argv and (
        any(Path(arg).name == "ledger_server.py" for arg in row.argv[:2])
        or row.argv[1:3] == ("-m", "scripts.swarm_ledger.ledger_server")
    )


def details(row: Process, proc: Path) -> dict:
    root = proc / str(row.pid)
    keys = {b"LEDGER_DIR", b"LEDGER_PORT", b"LEDGER_RUN_PID", b"LEDGER_RUN_START"}
    environ = {}
    for item in (root / "environ").read_bytes().split(b"\0"):
        key, _, value = item.partition(b"=")
        if key in keys:
            environ[key.decode()] = value.decode()
    cwd = os.readlink(root / "cwd")
    selected = Path(environ.get("LEDGER_DIR", str(Path.home() / "development-ledger"))).expanduser()
    folder = selected if selected.is_absolute() else Path(cwd) / selected
    environ["LEDGER_DIR"] = str(folder)
    identity = None
    if "LEDGER_RUN_START" in environ:
        identity = [int(environ["LEDGER_RUN_PID"]), int(environ["LEDGER_RUN_START"])]
    elif (folder / ".server.owner.json").exists():
        saved = json.loads((folder / ".server.owner.json").read_text())
        identity = [saved["pid"], saved["start"]]
    uptime = float((proc / "uptime").read_text().split()[0])
    return {
        "pid": row.pid,
        "port": address(environ)[1],
        "folder": str(folder),
        "cwd": cwd,
        "age": max(0, uptime - row.start_time / os.sysconf("SC_CLK_TCK")),
        "owner": identity,
    }


def orphan(row: Process, info: dict, table: dict[int, Process]) -> str:
    if shared(Path(info["folder"]), info["port"]):
        return ""
    if not Path(info["cwd"]).exists() or not Path(info["folder"]).exists():
        return "working folder is gone"
    if identity := info["owner"]:
        parent = table.get(identity[0])
        if parent is None or parent.start_time != identity[1] or parent.state == "Z":
            return "starting run ended"
    elif row.ppid == 1:
        return "starting run ended without an owner record"
    return ""


def terminate(row: Process, proc: Path) -> None:
    try:
        handle = os.pidfd_open(row.pid)
    except ProcessLookupError:
        return
    try:
        if (current := _process(row.pid, proc)) is None:
            return
        if current.start_time != row.start_time:
            raise ProcessLookupError("server identity changed")
        with suppress(ProcessLookupError):
            signal.pidfd_send_signal(handle, signal.SIGTERM)
            deadline = time.monotonic() + 1
            while current := _process(row.pid, proc):
                if current.start_time != row.start_time or current.state == "Z":
                    return
                if time.monotonic() >= deadline:
                    signal.pidfd_send_signal(handle, signal.SIGKILL)
                    return
                time.sleep(0.02)
    finally:
        os.close(handle)


def sweep_servers(
    table: dict[int, Process], home: Path, scope: str = "", act: bool = False, proc: Path = Path("/proc")
) -> list[dict]:
    records = []
    for row in table.values():
        if not server_process(row):
            continue
        try:
            info = details(row, proc)
            if scope and not any(Path(info[key]).is_relative_to(scope) for key in ("folder", "cwd")):
                continue
            if not (reason := orphan(row, info, table)):
                continue
            info.update(reason=reason, action="would stop")
            if act:
                terminate(row, proc)
                info["action"] = "stopped"
                with (home / "gc-ledger-servers.jsonl").open("a") as log:
                    log.write(json.dumps(info) + "\n")
            records.append(info)
        except (OSError, ValueError, KeyError) as exc:
            records.append({"pid": row.pid, "action": "error", "error": str(exc)})
    return records
