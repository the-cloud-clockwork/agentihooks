"""Live Claude sessions per OAuth account, read from /proc.

`agenti` leaves exactly one AH_CC_TOKEN_<slug> in a routed session's
environment, so the variable name identifies the account. Only variable names
are read; token values never leave this module.
"""

import hashlib
import os
from collections.abc import Iterable, Mapping
from pathlib import Path

TOKEN_PREFIX = "AH_CC_TOKEN_"
CODEX_TOKEN_PREFIX = "AH_CX_TOKEN_"
CODEX_DEFAULT = "default"
UNROUTED = "unrouted"
API_MARKER = "AH_ROUTE_API"
INTERACTIVE_PREFIX = "AH_ROUTE_INTERACTIVE_"
API_ACCOUNT = "api"
_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "fish", "ksh"})
_PROC = Path("/proc")
REMOTE = "--remote"
VALUE_FLAGS = frozenset(
    {
        "-c",
        "--config",
        "--enable",
        "--disable",
        REMOTE,
        "--remote-auth-token-env",
        "-i",
        "--image",
        "-m",
        "--model",
        "--local-provider",
        "-p",
        "--profile",
        "-s",
        "--sandbox",
        "-C",
        "--cd",
        "--add-dir",
        "-a",
        "--ask-for-approval",
    }
)
SERVERS = frozenset({"app-server", "mcp-server"})
INTERNAL_RUN = "AGENTIHOOKS_INTERNAL_RUN"
LISTEN = "--listen"
UNIX = "unix://"
MANAGED_RUNTIME = "cx"
MANAGED_SOCKET = "s"
_LAST_HELD: set[tuple[str, str]] = set()


def account_from_names(names: Iterable[str], prefix: str = TOKEN_PREFIX) -> str:
    names = list(names)
    if API_MARKER in names:
        return API_ACCOUNT
    interactive = {
        name.removeprefix(INTERACTIVE_PREFIX)
        for name in names
        if name.startswith(INTERACTIVE_PREFIX) and name != INTERACTIVE_PREFIX
    }
    if interactive:
        return interactive.pop() if len(interactive) == 1 else UNROUTED
    slugs = {name.removeprefix(prefix) for name in names if name.startswith(prefix) and name != prefix}
    return slugs.pop() if len(slugs) == 1 else UNROUTED


def codex_account_from_names(names: Iterable[str]) -> str:
    """A Codex process runs on a token account only when the router left it exactly one AH_CX_TOKEN_<slug>."""
    slug = account_from_names(names, CODEX_TOKEN_PREFIX)
    return CODEX_DEFAULT if slug == UNROUTED else slug


def environment_account(environ: Mapping[str, str] | None = None) -> str:
    active = os.environ if environ is None else environ
    return account_from_names(name for name, value in active.items() if value)


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode(errors="surrogateescape")).hexdigest()


def token_accounts(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Token fingerprint -> the slug the balancer routes that token under."""
    from scripts import operator_env

    active = {**(os.environ if environ is None else environ), **operator_env.block_tokens()}
    names = [
        name
        for name, value in active.items()
        if value and name.startswith(TOKEN_PREFIX) and name.removeprefix(TOKEN_PREFIX) not in ("", API_ACCOUNT)
    ]
    return {
        token_fingerprint(active[name]): name.removeprefix(TOKEN_PREFIX)
        for name in operator_env.distinct(active, names)
    }


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


def _env_fingerprint(pid: int, proc: Path, name: str) -> str:
    try:
        raw = (proc / str(pid) / "environ").read_bytes()
    except OSError:
        return ""
    for item in raw.split(b"\0"):
        key, _, value = item.partition(b"=")
        if key == name.encode() and value:
            return token_fingerprint(value.decode(errors="surrogateescape"))
    return ""


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


def pid_account(pid: int, proc: Path = _PROC, accounts: Mapping[str, str] | None = None) -> str:
    names = _env_names(pid, proc)
    if _is_codex(pid, proc):
        return codex_account_from_names(names)
    account = account_from_names(names)
    routed = token_accounts() if accounts is None else accounts
    return routed.get(_env_fingerprint(pid, proc, f"{TOKEN_PREFIX}{account}"), account)


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


def live_sessions(
    proc: Path = _PROC, exclude_pids: Iterable[int] = (), environ: Mapping[str, str] | None = None
) -> dict[int, str]:
    """PID -> account of every interactive Claude process on this host."""
    skip = set(exclude_pids)
    sessions = {}
    try:
        entries = [entry.name for entry in proc.iterdir() if entry.name.isdigit()]
    except OSError:
        return {}
    accounts = token_accounts(environ)
    for name in entries:
        pid = int(name)
        if pid not in skip and _is_interactive_claude(pid, proc):
            sessions[pid] = pid_account(pid, proc, accounts)
    return sessions


def _is_codex(pid: int, proc: Path) -> bool:
    argv = _cmdline(pid, proc)
    return bool(argv) and (_comm(pid, proc) == "codex" or Path(argv[0]).name == "codex")


def _codex_positionals(argv: list[str]) -> list[str]:
    rest = argv[1:]
    while rest and rest[0].startswith("-"):
        rest = rest[2:] if rest[0] in VALUE_FLAGS else rest[1:]
    return rest


def _is_codex_session(pid: int, proc: Path) -> bool:
    if not _is_codex(pid, proc):
        return False
    argv = _cmdline(pid, proc)
    if "app-server" in argv[0] or SERVERS.intersection(_codex_positionals(argv)[:1]):
        return False
    if any(part == REMOTE or part.startswith(f"{REMOTE}=") for part in argv[1:]):
        return False
    if INTERNAL_RUN in _env_names(pid, proc):
        return False
    return _comm(_ppid(pid, proc), proc) != "codex"


def execution_key(endpoint: str) -> str:
    if not endpoint.startswith(UNIX):
        return ""
    path = Path(endpoint)
    return path.parent.name if path.name == MANAGED_SOCKET and path.parent.parent.name == MANAGED_RUNTIME else ""


def _managed_execution(pid: int, proc: Path) -> str:
    if not _is_codex(pid, proc):
        return ""
    argv = _cmdline(pid, proc)
    if _codex_positionals(argv)[:1] != ["app-server"] or LISTEN not in argv[:-1]:
        return ""
    return execution_key(argv[argv.index(LISTEN) + 1])


def _codex_processes(proc: Path) -> tuple[dict[int, str], dict[tuple[str, str], int]]:
    """Codex sessions by pid, and each managed execution by execution key and account with its server pid."""
    sessions: dict[int, str] = {}
    managed: dict[tuple[str, str], int] = {}
    try:
        entries = sorted(int(entry.name) for entry in proc.iterdir() if entry.name.isdigit())
    except OSError:
        return sessions, managed
    for pid in entries:
        if _is_codex_session(pid, proc):
            sessions[pid] = codex_account_from_names(_env_names(pid, proc))
        elif key := _managed_execution(pid, proc):
            managed.setdefault((key, codex_account_from_names(_env_names(pid, proc))), pid)
    return sessions, managed


def live_codex_sessions(proc: Path = _PROC) -> int:
    """Interactive Codex sessions, exec runs and managed executions on this host; one CODEX_HOME is one account."""
    sessions, managed = _codex_processes(proc)
    return len(sessions) + len(managed)


def _held(raw: str) -> tuple[str, str] | None:
    from scripts.codex_session import binding

    try:
        record = binding.decode(raw)
    except (binding.BindingError, ValueError, TypeError, AttributeError):
        return raw, CODEX_DEFAULT
    if record.state in (binding.EXITED, binding.FAILED):
        return None
    return execution_key(record.endpoint_ref) or record.execution_id, record.account or CODEX_DEFAULT


def managed_held() -> set[tuple[str, str]]:
    """Transport records are written before the server starts; an unreadable record or store holds what it last held."""
    from hooks._redis import get_redis

    redis = get_redis()
    if redis is None:
        return set()
    from redis.exceptions import RedisError

    from scripts.codex_session import binding
    from scripts.swarm.store import RedisStore

    store = RedisStore(redis)
    try:
        raws = [raw for slug in store.slugs() for raw in redis.hvals(store.key(slug, binding.RECORDS))]
    except RedisError:
        return set(_LAST_HELD)
    _LAST_HELD.clear()
    _LAST_HELD.update(held for raw in raws if (held := _held(raw)))
    return set(_LAST_HELD)


def codex_sessions_by_account(proc: Path = _PROC, *, local: bool = False) -> dict[str, int]:
    """Codex sessions per account: a token slug or the default login; a managed execution counts once."""
    from scripts.routing import aggregate

    if not local and aggregate.enabled():
        import time

        return aggregate.counts("codex", time.time())
    sessions, managed = _codex_processes(proc)
    executions = set(managed) if local else set(managed) | managed_held()
    counts: dict[str, int] = {}
    for account in [*sessions.values(), *(account for _, account in executions)]:
        counts[account] = counts.get(account, 0) + 1
    return counts if local else _with_held(counts, hive_held("codex"))


def local_processes(proc: Path = _PROC) -> dict[str, dict[int, str]]:
    sessions, managed = _codex_processes(proc)
    executions = {pid: account for (_, account), pid in managed.items()}
    return {"claude": live_sessions(proc, handed_off_pids()), "codex": sessions | executions}


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


def hive_held(harness: str) -> dict[str, int]:
    """Other live hives' sessions and unreported launches per account, as the launch reservation counts them."""
    import time

    from redis.exceptions import RedisError

    from scripts.routing import aggregate
    from scripts.swarm.commands import hive_id
    from scripts.swarm.store import redis_client

    try:
        return aggregate.elsewhere(redis_client(), harness, time.time(), hive_id())
    except RedisError:
        return {}


def _with_held(counts: dict[str, int], held: dict[str, int]) -> dict[str, int]:
    for account, count in held.items():
        counts[account] = max(0, counts.get(account, 0) + count)
    return counts


def sessions_by_account(
    proc: Path = _PROC, *, local: bool = False, environ: Mapping[str, str] | None = None
) -> dict[str, int]:
    """Live sessions per account; sessions that already handed off their work do not hold a slot."""
    from scripts.routing import aggregate

    if not local and aggregate.enabled():
        import time

        return aggregate.counts("claude", time.time())
    try:
        skip = handed_off_pids()
    except Exception:
        skip = set()
    counts: dict[str, int] = {}
    for account in live_sessions(proc, skip, environ).values():
        counts[account] = counts.get(account, 0) + 1
    if not local:
        for account, held in fleet_held().items():
            counts[account] = counts.get(account, 0) + held
        counts = _with_held(counts, hive_held("claude"))
    return counts
