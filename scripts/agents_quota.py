import argparse
import json
import os
import shutil
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass

from scripts.claude_quota_balancer import _duration, _percent, _span
from scripts.session_caps import SessionCaps

PAGE_TTL_S = 60
REFRESH_MIN_S = 60
_page_cache: dict = {}
_last_refresh: dict = {}
_refresh_lock = threading.Lock()


@dataclass(frozen=True)
class QuotaRow:
    agent: str
    account: str
    state: str
    sessions: int
    five_hour_left: float | None
    seven_day_left: float | None
    seven_day_resets_at: int | None
    source: str
    five_hour_resets_at: int | None = None
    observed_at: float | None = None


def _left(used: float | None) -> float | None:
    return None if used is None else max(0.0, 100.0 - used)


def claude_rows(
    results: list, sessions: dict[str, int], source: str, observed: dict[str, float] | None = None
) -> list[QuotaRow]:
    return [
        QuotaRow(
            agent="claude",
            account=result.account,
            state=result.state,
            sessions=sessions.get(result.account, 0),
            five_hour_left=result.five_hour.remaining,
            seven_day_left=result.seven_day.remaining,
            seven_day_resets_at=result.seven_day.resets_at,
            source=source,
            five_hour_resets_at=result.five_hour.resets_at,
            observed_at=(observed or {}).get(result.account),
        )
        for result in results
    ]


def codex_rows(accounts: list, quotas: dict, sessions: dict[str, int], now: float) -> list[QuotaRow]:
    rows = []
    for account in accounts:
        quota = quotas.get(account.name)
        state = "SIGNED_OUT" if not account.signed_in else quota.state if quota else "UNKNOWN"
        rows.append(
            QuotaRow(
                agent="codex",
                account=account.name,
                state=state,
                sessions=sessions.get(account.name, 0),
                five_hour_left=_left(quota.five_hour.used) if quota else None,
                seven_day_left=_left(quota.seven_day.used) if quota else None,
                seven_day_resets_at=quota.seven_day.resets_at if quota else None,
                source=f"session-log {_span(max(0, int(now - quota.observed_at)))} ago" if quota else "no session log",
                five_hour_resets_at=quota.five_hour.resets_at if quota else None,
                observed_at=quota.observed_at if quota else None,
            )
        )
    return rows


def render(rows: list[QuotaRow], now: int, caps: Mapping[str, SessionCaps] | None = None) -> str:
    headers = ["AGENT", "ACCOUNT", "STATE", "SESSIONS", "5H LEFT", "5H RESET", "7D LEFT", "7D RESET", "SOURCE"]
    table = [
        [
            row.agent,
            row.account,
            row.state,
            f"{row.sessions}/{caps[row.agent].of(row.account)}" if caps and row.agent in caps else str(row.sessions),
            _percent(row.five_hour_left),
            _duration(row.five_hour_resets_at, now),
            _percent(row.seven_day_left),
            _duration(row.seven_day_resets_at, now),
            row.source,
        ]
        for row in rows
    ]
    widths = [max(len(cell) for cell in column) for column in zip(headers, *table)]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(line, widths)).rstrip() for line in [headers, *table]
    )


def _claude(refresh: bool, timeout: float) -> list[QuotaRow]:
    from hooks.context.account_sessions import sessions_by_account
    from scripts.claude_quota_balancer import cached_observations, collect_results, discover_credentials
    from scripts.install import _load_claude_runtime_env

    _load_claude_runtime_env()
    credentials = discover_credentials(os.environ)
    sessions = sessions_by_account()
    if credentials:
        results, source = collect_results(
            credentials, refresh=refresh, timeout=timeout, claude_bin=shutil.which("claude") or "claude"
        )
        return claude_rows(results, sessions, source)
    return claude_rows([result for _, result in cached_observations()], sessions, "cached")


def _codex(now: float) -> list[QuotaRow]:
    from hooks.context.account_sessions import codex_sessions_by_account
    from scripts import codex_router

    pool = codex_router.accounts(os.environ)
    return codex_rows(pool, codex_router.quotas(pool, os.environ), codex_sessions_by_account(), now)


def codex_table() -> str:
    from hooks.context.account_sessions import max_sessions
    from scripts import session_caps

    now = time.time()
    return render(_codex(now), int(now), {"codex": session_caps.caps(max_sessions(), "codex")})


def _page_quota(now: float) -> dict:
    from hooks.context import account_sessions
    from scripts import claude_quota_balancer, codex_router

    cached = claude_quota_balancer.cached_observations()
    observed = {result.account: at for at, result in cached}
    pool = codex_router.routing_pool(os.environ)
    claude = claude_rows([result for _, result in cached], account_sessions.sessions_by_account(), "cached", observed)
    rows = claude + codex_rows(
        pool, codex_router.quotas(pool, os.environ), account_sessions.codex_sessions_by_account(), now
    )
    return {
        "cap": account_sessions.max_sessions(),
        "probed_at": max(observed.values(), default=None),
        "rows": [asdict(row) for row in rows],
    }


def page_quota(now: float | None = None) -> dict:
    from scripts import session_caps

    now = time.time() if now is None else now
    if not _page_cache or now - _page_cache["at"] >= PAGE_TTL_S:
        _page_cache.update(at=now, quota=_page_quota(now))
    quota = _page_cache["quota"]
    caps = {harness: session_caps.stored(harness) for harness in session_caps.HARNESSES}
    rows = [{**row, "cap": caps[row["agent"]].get(row["account"], quota["cap"])} for row in quota["rows"]]
    return {**quota, "rows": rows}


def refresh_page_quota(probe: Callable[[], str], now: float | None = None) -> str:
    with _refresh_lock:
        now = time.time() if now is None else now
        if "at" in _last_refresh and now - _last_refresh["at"] < REFRESH_MIN_S:
            return ""
        error = probe()
        if not error:
            _last_refresh["at"] = now
            _page_cache.clear()
        return error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentihooks quota", description="Quota left for every agent harness")
    parser.add_argument("--json", action="store_true", help="Print rows as JSON")
    parser.add_argument("--refresh", action="store_true", help="Probe Claude accounts instead of using the cache")
    parser.add_argument("--timeout", type=float, default=60.0, help="Seconds per Claude account probe")
    args = parser.parse_args(argv)
    now = time.time()
    rows = _claude(args.refresh, args.timeout) + _codex(now)
    if args.json:
        print(json.dumps([asdict(row) for row in rows], indent=2))
    elif rows:
        print(render(rows, int(now)))
    else:
        print("agentihooks quota: no Claude account and no Codex session log found", file=sys.stderr)
    return 0 if rows else 1
