import os
import re
import shutil
import time
from pathlib import Path

from hooks.lifecycle.config import load_roots
from hooks.lifecycle.lease import add_holder, admin_dir, read_lease
from hooks.lifecycle.locks import being_removed
from hooks.lifecycle.model import Holder, Root
from hooks.proc import _process, _target

PATH_TOKEN = re.compile(r"(?:~|/)[^\s'\"`;|&<>()]+")
WARN_EVERY = 600
GB = 1 << 30


def _home() -> Path:
    from hooks.config import AGENTIHOOKS_HOME

    return Path(AGENTIHOOKS_HOME)


def _setting(key: str, default: str) -> str:
    return os.getenv(key, default)


def enabled() -> bool:
    return _setting("LIFECYCLE_GC_ENABLED", "true").lower() in ("1", "true", "yes")


def caller_holder(session_id: str, proc: Path = Path("/proc"), start: int | None = None) -> Holder | None:
    pid = os.getppid() if start is None else start
    try:
        boot = (proc / "sys" / "kernel" / "random" / "boot_id").read_text().strip()
    except OSError:
        boot = ""
    for _ in range(32):
        process = _process(pid, proc)
        if process is None:
            return None
        if _target(process):
            return Holder(session_id, pid, process.start_time, boot)
        if process.ppid in (0, pid):
            return None
        pid = process.ppid
    return None


def candidate_paths(tool_input: dict) -> list[str]:
    found = [str(tool_input[key]) for key in ("file_path", "notebook_path", "path") if tool_input.get(key)]
    found += PATH_TOKEN.findall(str(tool_input.get("command", "")))
    return [os.path.expanduser(item) for item in found]


def managed_unit(path: str, roots: list[Root]) -> tuple[Path, str] | None:
    target = Path(path)
    for root in roots:
        base = Path(root.path)
        if root.kind not in ("worktrees", "scratch") or not target.is_relative_to(base):
            continue
        parts = target.relative_to(base).parts
        depth = 3 if root.kind == "worktrees" and parts[1:2] == ("_tmp",) else 2
        if len(parts) < depth:
            return None
        unit = base.joinpath(*parts[:depth])
        if root.kind == "worktrees":
            return (unit, "worktree") if admin_dir(unit) else None
        return (unit, "scratch") if unit.is_dir() else None
    return None


def claim(payload: dict, roots: list[Root] | None = None, home: Path | None = None) -> str:
    paths = candidate_paths(payload.get("tool_input") or {})
    if not paths:
        return ""
    roots = load_roots() if roots is None else roots
    units = sorted({unit for path in paths if (unit := managed_unit(path, roots))})
    home = home or _home()
    session, holder = payload.get("session_id", ""), None
    for unit, kind in units:
        if being_removed(home, str(unit)):
            return f"BLOCKED: agentihooks gc is removing {unit} right now. Retry in a minute or work elsewhere."
        lease = read_lease(unit, kind)
        if lease and session and any(item.session_id == session for item in lease.holders):
            continue
        holder = holder or caller_holder(session)
        if holder:
            add_holder(unit, kind, holder, time.time())
    return ""


def free_gb() -> float:
    paths = [Path.home()] + ([Path("/mnt/c")] if Path("/mnt/c/Windows").is_dir() else [])
    sizes = []
    for path in paths:
        try:
            sizes.append(shutil.disk_usage(path).free)
        except OSError:
            continue
    return min(sizes) / GB if sizes else float("inf")


def kick(force: bool = False, home: Path | None = None) -> None:
    from hooks.lifecycle.timer import start_now

    stamp = (home or _home()) / "gc.stamp"
    interval = float(_setting("AGENTIHOOKS_GC_INTERVAL_MIN", "60")) * 60
    if not force and stamp.exists() and time.time() - stamp.stat().st_mtime < interval:
        return
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.touch()
    if start_now():
        return
    from hooks._async import fork_and_call
    from hooks.lifecycle.run import sweep

    fork_and_call(sweep, timeout_sec=3600, task_name="lifecycle-gc", act=True)


def disk_warning(home: Path | None = None) -> str:
    floor = float(_setting("AGENTIHOOKS_DISK_WARN_GB", "100"))
    free = free_gb()
    if free >= floor:
        return ""
    stamp = (home or _home()) / "gc-disk-warned"
    if stamp.exists() and time.time() - stamp.stat().st_mtime < WARN_EVERY:
        return ""
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.touch()
    kick(force=True, home=home)
    return (
        f"WARNING: disk space is low ({free:.0f} GB free, warning floor {floor:.0f} GB). "
        "A lifecycle sweep was started. Finish worktrees you no longer need with wt.sh done, "
        "remove scratch dirs with agentihooks scratch rm, and create no new worktrees until space returns."
    )


def pretool(payload: dict) -> str:
    if not enabled():
        return ""
    from hooks.common import inject_context, log

    try:
        warning, block = disk_warning(), claim(payload)
    except Exception as error:
        log("lifecycle pre-tool failed", {"error": str(error)})
        return ""
    if warning:
        inject_context(warning)
    return block


def session_event() -> None:
    if not enabled():
        return
    try:
        kick()
    except Exception as error:
        from hooks.common import log

        log("lifecycle session trigger failed", {"error": str(error)})
