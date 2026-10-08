"""Route a Codex launch to one account: the default `codex login` or an AH_CX_TOKEN_<slug>.

Token values only move from the environment into the child's CODEX_ACCESS_TOKEN;
they are never printed or written anywhere.
"""

import contextlib
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from hooks.context.account_sessions import (
    CODEX_DEFAULT,
    CODEX_TOKEN_PREFIX,
    TOKEN_PREFIX,
    codex_sessions_by_account,
)
from scripts import codex_quota, session_bands
from scripts.codex_quota import CodexQuota
from scripts.routing.slots import INTERACTIVE, SUBSCRIPTION, Slot

TOKEN_ENV = "CODEX_ACCESS_TOKEN"
PROBE_ARGS = ["exec", "--json", "--skip-git-repo-check", "Reply with the single word ok."]
PROBE_TIMEOUT_S = 90
CODEX_BIN = "codex"
ATTEMPTS_FILE = "codex-probe-attempts.json"
# A running app-server daemon answers account/read with its own auth, so a token session must not attach to it.
NO_DAEMON = "--no-daemon"


@dataclass(frozen=True)
class CodexAccount:
    name: str
    env_name: str = ""
    signed_in: bool = True

    @property
    def is_token(self) -> bool:
        return bool(self.env_name)


class RoutingError(RuntimeError):
    pass


def token_accounts(environ: Mapping[str, str]) -> list[CodexAccount]:
    return [
        CodexAccount(name.removeprefix(CODEX_TOKEN_PREFIX), name)
        for name in sorted(environ)
        if name.startswith(CODEX_TOKEN_PREFIX) and name != CODEX_TOKEN_PREFIX and environ[name]
    ]


def _without_tokens(environ: Mapping[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in environ.items()
        if name != TOKEN_ENV and not name.startswith((CODEX_TOKEN_PREFIX, TOKEN_PREFIX))
    }


def default_signed_in(environ: Mapping[str, str], run: Callable = subprocess.run) -> bool:
    try:
        status = run(
            [shutil.which("codex") or "codex", "login", "status"],
            env=_without_tokens(environ),
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return status.returncode == 0


def accounts(environ: Mapping[str, str], run: Callable = subprocess.run) -> list[CodexAccount]:
    return [CodexAccount(CODEX_DEFAULT, signed_in=default_signed_in(environ, run)), *token_accounts(environ)]


def routing_pool(environ: Mapping[str, str], run: Callable = subprocess.run) -> list[CodexAccount]:
    """Without token variables only the default login routes, unchecked, exactly as before tokens existed."""
    return accounts(environ, run) if token_accounts(environ) else [CodexAccount(CODEX_DEFAULT)]


def _registry() -> dict:
    from hooks.context.broadcast import _load_sessions

    return _load_sessions()


def quotas(pool: list[CodexAccount], environ: Mapping[str, str]) -> dict[str, CodexQuota | None]:
    """Each account's newest quota, from the session logs the session registry attributes to it."""
    owner = {session_id: info.get("account") for session_id, info in _registry().items()}
    tokens = {account.name for account in pool if account.is_token}

    def keep(account: CodexAccount) -> Callable[[str], bool]:
        if account.is_token:
            return lambda session_id: owner.get(session_id) == account.name
        return lambda session_id: owner.get(session_id) not in tokens

    return {account.name: codex_quota.latest_codex_quota(dict(environ), keep(account)) for account in pool}


def account_cap(quota: CodexQuota | None, now: float) -> int | None:
    """Live sessions under the session bands: the week alone sets it; None without a fresh reading."""
    if quota is None or not session_bands.fresh(quota.observed_at, now):
        return None
    return session_bands.week_cap(session_bands.left(quota.seven_day.used, quota.seven_day.resets_at, now))


def _thread(stdout: str) -> str:
    for line in stdout.splitlines():
        with contextlib.suppress(ValueError):
            event = json.loads(line)
            if isinstance(event, dict) and event.get("type") == "thread.started":
                return str(event.get("thread_id") or "")
    return ""


def probe(account: CodexAccount, environ: Mapping[str, str], run: Callable = subprocess.run) -> CodexQuota | None:
    """A fresh reading from one tiny exec on the account, read from the rollout it writes."""
    codex_bin = shutil.which(CODEX_BIN) or CODEX_BIN
    try:
        done = run(
            command(account, codex_bin, PROBE_ARGS),
            env=child_environment(account, environ),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    thread = _thread(done.stdout) if done.stdout else ""
    return codex_quota.session_quota(dict(environ), thread) if thread else None


def _attempts_path() -> Path:
    return Path.home() / ".agentihooks" / ATTEMPTS_FILE


def _probe_attempts() -> dict[str, float]:
    with contextlib.suppress(OSError, ValueError):
        return json.loads(_attempts_path().read_text())
    return {}


def fresh_quotas(
    pool: list[CodexAccount], environ: Mapping[str, str], now: float, run: Callable = subprocess.run
) -> dict[str, CodexQuota | None]:
    """Each account's newest reading, probed again when it is missing or stale, at most once per freshness window."""
    found = quotas(pool, environ)
    attempts = _probe_attempts()
    for account in pool:
        seen = found.get(account.name)
        stale = not session_bands.fresh(seen.observed_at if seen else None, now)
        if account.signed_in and stale and not session_bands.fresh(attempts.get(account.name), now):
            attempts[account.name] = now
            found[account.name] = probe(account, environ, run) or seen
    with contextlib.suppress(OSError):
        _attempts_path().write_text(json.dumps(attempts))
    return found


@dataclass(frozen=True)
class CodexAccountSource:
    run: Callable | None = None
    sessions: Mapping[str, int] = field(default_factory=dict)
    refresh: bool = True

    def _run(self) -> tuple[Callable, ...]:
        return () if self.run is None else (self.run,)

    def pool(self, environ: Mapping[str, str]) -> list[CodexAccount]:
        return routing_pool(environ, *self._run())

    def readings(self, pool: list[CodexAccount], environ: Mapping[str, str], now: float) -> dict:
        return fresh_quotas(pool, environ, now, *self._run()) if self.refresh else quotas(pool, environ)

    def slots(self, environ: Mapping[str, str], now: float) -> list[Slot]:
        pool = self.pool(environ)
        return seats(pool, self.readings(pool, environ, now), self.sessions, now)

    def child_env(self, slot: Slot, environ: Mapping[str, str]) -> dict[str, str]:
        tokens = {account.name: account for account in token_accounts(environ)}
        account = CodexAccount(CODEX_DEFAULT) if slot.kind == INTERACTIVE else tokens[slot.account]
        return child_environment(account, environ)


def select(
    pool: list[CodexAccount],
    quotas: Mapping[str, CodexQuota | None],
    sessions: Mapping[str, int],
    now: float,
    route: str = "",
) -> tuple[CodexAccount, str, session_bands.Seat | None]:
    """The signed in account with a free place under its band and the fewest sessions."""
    if route:
        for account in pool:
            if account.name == route:
                return account, "forced", None
        available = ", ".join(account.name for account in pool)
        raise RoutingError(f"Codex account '{route}' not found; available: {available}")
    by_name = {account.name: account for account in pool}
    seat = session_bands.pick(seats(pool, quotas, sessions, now))
    if seat is None:
        raise RoutingError("no signed in Codex account has a fresh reading and a free session under its quota band")
    return by_name[seat.account], "open", seat


def seats(
    pool: list[CodexAccount], quotas: Mapping[str, CodexQuota | None], sessions: Mapping[str, int], now: float
) -> list[session_bands.Seat]:
    return [
        Slot(
            "codex",
            account.name,
            cap,
            sessions.get(account.name, 0),
            _spend_by(quota, now),
            kind=SUBSCRIPTION if account.is_token else INTERACTIVE,
        )
        for account in pool
        if account.signed_in and (cap := account_cap(quota := quotas.get(account.name), now)) is not None
    ]


def _spend_by(quota: CodexQuota, now: float) -> float | None:
    five = session_bands.left(quota.five_hour.used, quota.five_hour.resets_at, now)
    return session_bands.spend_by(five, session_bands.upcoming(quota.seven_day.resets_at, now))


def child_environment(account: CodexAccount, environ: Mapping[str, str]) -> dict[str, str]:
    child = _without_tokens(environ)
    if account.is_token:
        child[account.env_name] = environ[account.env_name]
        child[TOKEN_ENV] = environ[account.env_name]
    return child


def command(account: CodexAccount, codex_bin: str, args: list[str]) -> list[str]:
    return [codex_bin, *([NO_DAEMON] if account.is_token else []), *args]


def _take(args: list[str], flag: str) -> tuple[str, list[str]]:
    value, rest, index = "", [], 0
    while index < len(args):
        if args[index] == flag and index + 1 < len(args):
            value, index = args[index + 1], index + 2
            continue
        if args[index].startswith(f"{flag}="):
            value, index = args[index].split("=", 1)[1], index + 1
            continue
        rest.append(args[index])
        index += 1
    return value, rest


def _report(path: str, **fields: str) -> None:
    if path:
        from scripts.install import _write_route_report

        _write_route_report(path, **fields)


def _route(environ: Mapping[str, str], route: str, run: Callable) -> tuple[CodexAccount, str, int, str]:
    sessions = codex_sessions_by_account()
    source = CodexAccountSource(run, sessions, refresh=not route)
    pool = source.pool(environ)
    now = time.time()
    found = source.readings(pool, environ, now)
    account, placement, seat = select(pool, found, sessions, now, route)
    return account, placement, sessions.get(account.name, 0), str(seat.cap) if seat else "?"


def main(
    argv: list[str],
    environ: Mapping[str, str] | None = None,
    execvpe: Callable = os.execvpe,
    run: Callable = subprocess.run,
) -> int:
    active = dict(os.environ if environ is None else environ)
    report, args = _take(argv, "--agentihooks-report")
    route, args = _take(args, "--route")
    lock_path = Path(active.get("HOME", str(Path.home()))) / ".agentihooks" / "codex-route.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Held until exec: the descriptor is close-on-exec, so the next launch counts this one as a live session.
    lock = lock_path.open("a+", encoding="utf-8")
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        account, placement, live, cap = _route(active, route, run)
    except RoutingError as exc:
        print(f"agentihooks codex: {exc}", file=sys.stderr)
        _report(report, status="failed", error=str(exc))
        return 3
    _report(report, status="routed", account=account.name, placement=placement)
    print(f"[agentihooks codex] account={account.name} sessions={live}/{cap} placement={placement}", flush=True)
    codex_bin = shutil.which("codex") or "codex"
    execvpe(codex_bin, command(account, codex_bin, args), child_environment(account, active))
    return 0
