"""Close the herdr panes agentihooks launched once their agent is gone. A pane it never recorded is never read or
closed, and nothing closes inside the launch grace, while its input line holds text or inside the quiet window."""

import hashlib
import os
import re
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from scripts import herdr_host, herdr_panes
from scripts.herdr_host import HerdrError
from scripts.herdr_panes import PaneRecord
from scripts.inbox import wake
from scripts.swarm import idle
from scripts.swarm.pane import typed_input

GRACE_ENV = "AGENTIHOOKS_HERDR_GC_GRACE_MINUTES"
DEFAULT_GRACE_MINUTES = 5
RUN_FILE_AGE_S = 24 * 3600
RUN_SUFFIXES = (".sh", ".route", ".prompt", ".started")
SHELLS = frozenset({"bash", "zsh", "sh", "dash", "fish", "ksh", "tcsh", "csh"})
SHELL_TYPED = re.compile(r"[$#%>]\s+\S")
STARTED_ROUTES = ("routed", "bare", "direct")
CLOSE, KEEP, FORGET = "close", "keep", "forget"
NO_SWARM, LIVE, RETIRED, STOPPED, REMOVED, UNKNOWN = "none", "live", "retired", "stopped", "removed", "unknown"
GONE = {RETIRED: "its agent was retired", STOPPED: "its swarm stopped", REMOVED: "its swarm was removed"}
READ_ERRORS = (HerdrError, OSError, subprocess.TimeoutExpired)


@dataclass(frozen=True)
class Finding:
    record: PaneRecord
    action: str
    reason: str


@dataclass(frozen=True)
class Emptied:
    kind: str
    ident: str


@dataclass(frozen=True)
class Context:
    environ: dict
    now_ms: int
    act: bool
    herdr: object
    owners: object


class Herdr:
    def __init__(self, environ: dict[str, str]):
        self.environ = environ

    def _call(self, *args: str) -> dict:
        return herdr_host._cli(list(args), self.environ)

    def list_panes(self) -> dict[str, dict]:
        return {pane["pane_id"]: pane for pane in self._call("pane", "list").get("panes", [])}

    def process_info(self, pane_id: str) -> dict:
        return self._call("pane", "process-info", "--pane", pane_id).get("process_info", {})

    def screen(self, pane_id: str) -> str:
        return self._call("pane", "read", pane_id, "--source", "visible", "--format", "text").get("text", "")

    def close_pane(self, pane_id: str) -> None:
        self._call("pane", "close", pane_id)

    def tabs(self) -> list[dict]:
        return self._call("tab", "list").get("tabs", [])

    def workspaces(self) -> list[dict]:
        return self._call("workspace", "list").get("workspaces", [])

    def close_tab(self, tab_id: str) -> None:
        self._call("tab", "close", tab_id)

    def close_workspace(self, workspace_id: str) -> None:
        self._call("workspace", "close", workspace_id)


def _connect(url: str):
    from scripts.swarm.store import RedisStore, redis_client

    return RedisStore(redis_client({"AGENTIHOOKS_SWARM_REDIS_URL": url} if url else None))


def _fate(store, record: PaneRecord) -> str:
    slug = record.owner_swarm
    if slug not in store.slugs():
        return REMOVED
    if store.config(slug).state == "stopped":
        return STOPPED
    names = {agent.name for agent in store.agents(slug) if agent.state != "finished"}
    return LIVE if record.owner_session in names else RETIRED


class Owners:
    """What became of a recorded pane's swarm agent, read from the swarm store it was launched against."""

    def __init__(self, connect=_connect):
        self.connect, self.stores = connect, {}

    def _store(self, record: PaneRecord):
        url = record.swarm_store
        if not record.owner_swarm or url == herdr_panes.WITHHELD:
            return None
        if url not in self.stores:
            try:
                self.stores[url] = self.connect(url)
            except Exception:
                self.stores[url] = None
        return self.stores[url]

    def state(self, record: PaneRecord) -> str:
        if not record.owner_swarm:
            return NO_SWARM
        store = self._store(record)
        try:
            return UNKNOWN if store is None else _fate(store, record)
        except Exception:
            return UNKNOWN

    def prompted_at(self, record: PaneRecord) -> int | None:
        store = self._store(record)
        try:
            return None if store is None else idle.last_prompt(store.redis, record.owner_swarm, record.owner_session)
        except Exception:
            return None


def grace_ms(environ: dict[str, str]) -> int:
    return int(float(environ.get(GRACE_ENV) or DEFAULT_GRACE_MINUTES) * 60_000)


def digest(screen: str) -> str:
    return hashlib.sha256(screen.encode()).hexdigest()


def _is_shell(process: dict) -> bool:
    argv = process.get("argv") or []
    return process.get("name") in SHELLS and all(arg.startswith("-") for arg in argv[1:])


def bare_shell(info: dict) -> bool:
    found = info.get("foreground_processes") or []
    return bool(found) and all(_is_shell(process) for process in found)


def _typed(screen: str, bare: bool) -> bool:
    if not bare:
        return bool(typed_input(screen))
    lines = [line for line in screen.splitlines() if line.strip()]
    return bool(lines) and bool(SHELL_TYPED.search(lines[-1]))


def _gone(record: PaneRecord, bare: bool, state: str) -> str:
    if state in GONE:
        return GONE[state]
    if not bare:
        return ""
    if record.kind == "run-in-terminal":
        return "its command exited and left a bare shell"
    if record.route_status in STARTED_ROUTES:
        return "its agent exited and left a bare shell"
    return "a failed launch left it at a bare shell"


def _forget(record: PaneRecord, ctx: Context, reason: str) -> Finding:
    if ctx.act:
        herdr_panes.forget(record, ctx.environ)
    return Finding(record, FORGET, reason)


def _look(record: PaneRecord, ctx: Context) -> Finding:
    state = ctx.owners.state(record)
    if state == UNKNOWN:
        return Finding(record, KEEP, "its swarm store cannot be read")
    bare = bare_shell(ctx.herdr.process_info(record.pane_id))
    reason = _gone(record, bare, state)
    if not reason:
        return Finding(record, KEEP, "its agent is live")
    screen = ctx.herdr.screen(record.pane_id)
    if _typed(screen, bare):
        return Finding(record, KEEP, "its input line holds text")
    seen = digest(screen)
    active = record.active_at if seen == record.seen else ctx.now_ms
    if seen != record.seen:
        herdr_panes.update(record, ctx.environ, seen=seen, active_at=active)
    if ctx.now_ms - max(active, ctx.owners.prompted_at(record) or 0) < wake.quiet_ms(ctx.environ):
        return Finding(record, KEEP, "used inside the quiet window")
    if ctx.act:
        ctx.herdr.close_pane(record.pane_id)
        herdr_panes.forget(record, ctx.environ)
    return Finding(record, CLOSE, reason)


def judge(record: PaneRecord, pane: dict | None, ctx: Context) -> Finding:
    if pane is None:
        return _forget(record, ctx, "herdr no longer lists it")
    if pane.get("terminal_id") != record.terminal_id:
        return _forget(record, ctx, "its pane id now holds another terminal")
    if ctx.now_ms - record.launched_at < grace_ms(ctx.environ):
        return Finding(record, KEEP, "inside the launch grace")
    try:
        return _look(record, ctx)
    except READ_ERRORS as exc:
        return Finding(record, KEEP, f"herdr could not read it: {exc}")


def _close_emptied(closed: list[PaneRecord], herdr) -> list[Emptied]:
    tabs, spaces = {r.tab_id for r in closed}, {r.workspace_id for r in closed}
    done = []
    try:
        for tab in herdr.tabs():
            if tab["tab_id"] in tabs and not tab.get("pane_count"):
                herdr.close_tab(tab["tab_id"])
                done.append(Emptied("tab", tab["tab_id"]))
        for space in herdr.workspaces():
            if space["workspace_id"] in spaces and not space.get("pane_count"):
                herdr.close_workspace(space["workspace_id"])
                done.append(Emptied("workspace", space["workspace_id"]))
    except READ_ERRORS:
        pass
    return done


def sweep(environ: dict[str, str], now_ms: int, act: bool, herdr: Herdr, owners: Owners) -> list:
    ctx = Context(environ, now_ms, act, herdr, owners)
    panes = herdr.list_panes()
    found = [judge(record, panes.get(record.pane_id), ctx) for record in herdr_panes.load(environ)]
    closed = [f.record for f in found if f.action == CLOSE]
    return found + (_close_emptied(closed, herdr) if act and closed else [])


def lines(findings: list, act: bool) -> list[str]:
    out = []
    for found in findings:
        if isinstance(found, Emptied):
            out.append(f"closed {found.kind} {found.ident}, left empty")
        elif found.action == CLOSE:
            owner = found.record.owner_session or "no session"
            out.append(f"{'closed' if act else 'would close'} pane {found.record.pane_id} of {owner}: {found.reason}")
    return out


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _launch_file(path: Path, alive) -> bool:
    if path.name.startswith("closing-"):
        pid = path.name.removeprefix("closing-")
        return pid.isdigit() and not alive(int(pid))
    return path.suffix in RUN_SUFFIXES or (path.name.startswith("profile-") and path.suffix == ".json")


def _older(path: Path, now_s: float) -> bool:
    try:
        found = path.stat()
    except OSError:
        return False
    return stat.S_ISREG(found.st_mode) and now_s - found.st_mtime > RUN_FILE_AGE_S


def stale_launch_files(folder: Path, now_s: float, alive: Callable[[int], bool] = _alive) -> list[Path]:
    if not folder.is_dir():
        return []
    return [path for path in sorted(folder.iterdir()) if _older(path, now_s) and _launch_file(path, alive)]


def clean_run_dir(folder: Path, now_s: float, act: bool, alive: Callable[[int], bool] = _alive) -> list[str]:
    out = []
    for path in stale_launch_files(folder, now_s, alive):
        if act:
            path.unlink(missing_ok=True)
        out.append(f"{'removed' if act else 'would remove'} {path.name}")
    return out


def run(environ: dict[str, str], now_ms: int, act: bool) -> list[str]:
    out = clean_run_dir(herdr_panes.run_folder(environ), now_ms / 1000, act)
    if not herdr_host.binary() or not herdr_host.server_running(environ):
        return out
    try:
        return out + lines(sweep(environ, now_ms, act, Herdr(environ), Owners()), act)
    except READ_ERRORS as exc:
        return [*out, f"herdr sweep failed: {exc}"]
