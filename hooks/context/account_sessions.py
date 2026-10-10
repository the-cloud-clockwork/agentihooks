"""Live Claude sessions per OAuth account, read from /proc.

`agenti` leaves exactly one AH_CC_TOKEN_<slug> in a routed session's
environment, so the variable name identifies the account. Only variable names
are read; token values never leave this module.
"""

import os
from collections.abc import Iterable, Mapping
from pathlib import Path

TOKEN_PREFIX = "AH_CC_TOKEN_"
CODEX_TOKEN_PREFIX = "AH_CX_TOKEN_"
CODEX_DEFAULT = "default"
UNROUTED = "unrouted"
API_MARKER = "AH_ROUTE_API"
API_ACCOUNT = "api"
_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "fish", "ksh"})
_PROC = Path("/proc")
ROUTER_FLAGS = ("--no-daemon",)
CONFIG_FLAGS = ("-c", "--config")
CONFIG_PREFIXES = ("-c=", "--config=")
NON_INTERACTIVE = ("exec", "app-server", "mcp-server")


def account_from_names(names: Iterable[str], prefix: str = TOKEN_PREFIX) -> str:
    """API_ACCOUNT under the route marker, else the one slug among names carrying ``prefix``; UNROUTED when there is none or more than one."""
    names = list(names)
    if API_MARKER in names:
        return API_ACCOUNT
    slugs = {name.removeprefix(prefix) for name in names if name.startswith(prefix) and name != prefix}
    return slugs.pop() if len(slugs) == 1 else UNROUTED


def codex_account_from_names(names: Iterable[str]) -> str:
    """A Codex process runs on a token account only when the router left it exactly one AH_CX_TOKEN_<slug>."""
    slug = account_from_names(names, CODEX_TOKEN_PREFIX)
    return CODEX_DEFAULT if slug == UNROUTED else slug


def environment_account(environ: Mapping[str, str] | None = None) -> str:
    active = os.environ if environ is None else environ
    return account_from_names(name for name, value in active.items() if value)


def _comm(pid: int, proc: Path) -> str | None:
    try:
        return (proc / str(pid) / "comm").read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _ppid(pid: int, proc: Path) -> int:
    try:
        return int((proc / str(pid) / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return 0


def _cmdline(pid: int, proc: Path) -> list[str]:
    try:
        raw = (proc / str(pid) / "cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode(errors="replace") for part in raw.split(b"\0") if part]


def _env_names(pid: int, proc: Path) -> list[str]:
    try:
        raw = (proc / str(pid) / "environ").read_bytes()
    except OSError:
        return []
    names = []
    for item in raw.split(b"\0"):
        name, _, value = item.partition(b"=")
        if name and value:
            names.append(name.decode(errors="replace"))
    return names


def agent_pid(start: int | None = None, proc: Path = _PROC) -> int:
    """The nearest ancestor that is not a shell: the agent CLI that ran this hook or command."""
    first = os.getppid() if start is None else start
    pid = first
    while pid > 1:
        comm = _comm(pid, proc)
        if comm is None:
            return first
        if comm not in _SHELLS:
            return pid
        pid = _ppid(pid, proc)
    return first


def pid_account(pid: int, proc: Path = _PROC) -> str:
    names = _env_names(pid, proc)
    return codex_account_from_names(names) if _is_codex(pid, proc) else account_from_names(names)


def session_account(pid: int, proc: Path = _PROC) -> str:
    """Account of the agent process ``pid``; this process's environment when /proc is unavailable."""
    if (proc / str(pid) / "environ").exists():
        return pid_account(pid, proc)
    return environment_account()


def _is_interactive_claude(pid: int, proc: Path) -> bool:
    argv = _cmdline(pid, proc)
    if not argv:
        return False
    launcher = _comm(pid, proc) == "claude" or Path(argv[0]).name == "claude"
    launcher = launcher or any(part.endswith("claude-code/cli.js") for part in argv[:2])
    return launcher and not any(part in ("-p", "--print") for part in argv[1:])


def live_sessions(proc: Path = _PROC, exclude_pids: Iterable[int] = ()) -> dict[int, str]:
    """PID -> account of every interactive Claude process on this host."""
    skip = set(exclude_pids)
    sessions = {}
    try:
        entries = [entry.name for entry in proc.iterdir() if entry.name.isdigit()]
    except OSError:
        return {}
    for name in entries:
        pid = int(name)
        if pid not in skip and _is_interactive_claude(pid, proc):
            sessions[pid] = pid_account(pid, proc)
    return sessions


def _is_codex(pid: int, proc: Path) -> bool:
    argv = _cmdline(pid, proc)
    return bool(argv) and (_comm(pid, proc) == "codex" or Path(argv[0]).name == "codex")


def _codex_subcommand(argv: list[str]) -> str:
    """The first argument after the options the router puts ahead of it: --no-daemon and -c key=value overrides."""
    rest = argv[1:]
    while rest and (rest[0] in ROUTER_FLAGS + CONFIG_FLAGS or rest[0].startswith(CONFIG_PREFIXES)):
        rest = rest[2:] if rest[0] in CONFIG_FLAGS else rest[1:]
    return rest[0] if rest else ""


def _is_interactive_codex(pid: int, proc: Path) -> bool:
    if not _is_codex(pid, proc):
        return False
    argv = _cmdline(pid, proc)
    if "app-server" in argv[0] or _codex_subcommand(argv) in NON_INTERACTIVE:
        return False
    return _comm(_ppid(pid, proc), proc) != "codex"


def live_codex_sessions(proc: Path = _PROC) -> int:
    """Interactive Codex sessions on this host; one CODEX_HOME is one account."""
    try:
        entries = [int(entry.name) for entry in proc.iterdir() if entry.name.isdigit()]
    except OSError:
        return 0
    return sum(1 for pid in entries if _is_interactive_codex(pid, proc))


def codex_sessions_by_account(proc: Path = _PROC) -> dict[str, int]:
    """Interactive Codex sessions per Codex account: a token slug, or the default login."""
    try:
        entries = [int(entry.name) for entry in proc.iterdir() if entry.name.isdigit()]
    except OSError:
        return {}
    counts: dict[str, int] = {}
    for pid in entries:
        if _is_interactive_codex(pid, proc):
            account = codex_account_from_names(_env_names(pid, proc))
            counts[account] = counts.get(account, 0) + 1
    return counts


def handed_off_pids() -> set[int]:
    from hooks.context.broadcast import _load_sessions

    return {
        int(info["pid"])
        for info in _load_sessions().values()
        if info.get("status") == "handed_off" and isinstance(info.get("pid"), int)
    }


def fleet_held(environ: Mapping[str, str] | None = None) -> dict[str, int]:
    """Account slots held across the fleet that this host's /proc cannot see; none while distributed launches are off."""
    from scripts.swarm_v2.runtime.base import LOCAL, RuntimeRouter

    if RuntimeRouter.from_environ((), os.environ if environ is None else environ).spawn_backend() == LOCAL:
        return {}
    from hooks._redis import get_redis

    redis = get_redis()
    if redis is None:
        return {}
    from redis.exceptions import RedisError

    from scripts.swarm import lease
    from scripts.swarm.store import RedisStore
    from scripts.swarm_v2 import accounts

    store = RedisStore(redis)
    try:
        return accounts.fleet_held(store, lease.now_ms(store))
    except RedisError:
        return {}


def sessions_by_account(proc: Path = _PROC) -> dict[str, int]:
    """Live sessions per account; sessions that already handed off their work do not hold a slot."""
    try:
        skip = handed_off_pids()
    except Exception:
        skip = set()
    counts: dict[str, int] = {}
    for account in live_sessions(proc, skip).values():
        counts[account] = counts.get(account, 0) + 1
    for account, held in fleet_held().items():
        counts[account] = counts.get(account, 0) + held
    return counts
