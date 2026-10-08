"""Route a Codex launch to one account: the default `codex login` or an AH_CX_TOKEN_<slug>.

Token values only move from the environment into the child's CODEX_ACCESS_TOKEN;
they are never printed or written anywhere.
"""

import fcntl
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from hooks.context.account_sessions import (
    CODEX_DEFAULT,
    CODEX_TOKEN_PREFIX,
    TOKEN_PREFIX,
    codex_sessions_by_account,
    max_sessions,
)
from scripts import codex_quota, quota_pace, session_caps
from scripts.claude_quota_balancer import QuotaWindow
from scripts.codex_quota import CodexQuota

TOKEN_ENV = "CODEX_ACCESS_TOKEN"
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


def windows(quota: CodexQuota, now: float) -> tuple[QuotaWindow, QuotaWindow]:
    """(five hour, week) with each reset passed read as empty; a fresh weekly only reading has no five hour limit."""
    five, week = (
        QuotaWindow(used=0, resets_at=None) if window.resets_at and window.resets_at <= now else window
        for window in (quota.five_hour, quota.seven_day)
    )
    if five.used is None and now - quota.observed_at < codex_quota.FIVE_HOUR_MINUTES * 60:
        five = QuotaWindow(used=0)
    return five, week


def spendable_rate(quota: CodexQuota | None, now: float) -> float | None:
    if quota is None:
        return None
    five, week = windows(quota, now)
    return quota_pace.rate(five, [week], now)


def _admits(quota: CodexQuota | None, now: float) -> bool:
    if spendable_rate(quota, now) is None:
        return True
    five, week = windows(quota, now)
    return quota_pace.routable(five, [week], now)


def _rank(pool: list[CodexAccount], quotas: Mapping[str, CodexQuota | None], now: float) -> list[CodexAccount]:
    def key(account: CodexAccount):
        rate = spendable_rate(quotas.get(account.name), now)
        return (rate is None, -(rate or 0.0), account.name)

    return sorted(pool, key=key)


def select(
    pool: list[CodexAccount],
    quotas: Mapping[str, CodexQuota | None],
    sessions: Mapping[str, int],
    cap: int,
    route: str = "",
    caps: Mapping[str, int] | None = None,
    now: float | None = None,
) -> tuple[CodexAccount, str]:
    """(account, placement): the highest spendable rate below the cap, the least loaded when all are full."""
    if route:
        for account in pool:
            if account.name == route:
                return account, "forced"
        available = ", ".join(account.name for account in pool)
        raise RoutingError(f"Codex account '{route}' not found; available: {available}")
    timestamp = time.time() if now is None else now
    eligible = [account for account in pool if account.signed_in and _admits(quotas.get(account.name), timestamp)]
    if not eligible:
        raise RoutingError("no Codex account is signed in with routing left")
    below = [account for account in eligible if sessions.get(account.name, 0) < (caps or {}).get(account.name, cap)]
    if below:
        return _rank(below, quotas, timestamp)[0], "open"
    return min(_rank(eligible, quotas, timestamp), key=lambda account: sessions.get(account.name, 0)), "overflow"


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


def _route(environ: Mapping[str, str], route: str, run: Callable) -> tuple[CodexAccount, str, int, int]:
    cap = max_sessions(environ)
    sessions = codex_sessions_by_account()
    caps = session_caps.stored("codex")
    if not token_accounts(environ) and not route:
        return CodexAccount(CODEX_DEFAULT), "open", sessions.get(CODEX_DEFAULT, 0), caps.get(CODEX_DEFAULT, cap)
    pool = routing_pool(environ, run)
    account, placement = select(pool, quotas(pool, environ), sessions, cap, route, caps)
    return account, placement, sessions.get(account.name, 0), caps.get(account.name, cap)


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
