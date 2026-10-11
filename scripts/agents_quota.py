import argparse
import json
import os
import shutil
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING

from scripts import codex_router, session_bands
from scripts.claude_quota_balancer import (
    MASTERS_ONLY,
    OPEN,
    QuotaWindow,
    _cap_text,
    _duration,
    _weight_text,
    account_cap,
)
from scripts.routing.slots import API, INTERACTIVE, SUBSCRIPTION, Slot

if TYPE_CHECKING:
    from scripts.codex_quota import CodexQuota
    from scripts.routing.master_account import MasterAccount
    from scripts.routing.slots import SlotSource

PAGE_TTL_S = 60
STALE = "STALE"
EXPIRED_SOURCE = "token expired: run {login}"
NEXT_MARK = "(next)"
CURRENT_MARK = "(current)"
NO_CURRENT = ""
CACHED = "cached"
FABLE_COLUMNS = ["FABLE LEFT", "FABLE RESET"]
MISSING = "-"
HARNESSES = ("claude", "codex")
UNLISTED = "+{count} sessions on unlisted account names"
COLUMNS = [
    "#",
    "HARNESS",
    "ACCOUNT",
    "KIND",
    "STATE",
    "SESSIONS",
    "WEIGHT",
    "CAP",
    "ROUTING LEFT",
    "5H LEFT",
    "5H RESET",
    "7D LEFT",
    "7D RESET",
]
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
    cap: int | None = None
    kind: str = SUBSCRIPTION
    weight: int | None = None
    master: str = ""
    selected: bool = False
    routing_left: float | None = None
    fable_left: float | None = None
    fable_resets_at: int | None = None


def claude_rows(
    results: list,
    sessions: dict[str, int],
    source: str = CACHED,
    observed: dict[str, float] | None = None,
    now: float | None = None,
    include_fable: bool = False,
) -> list[QuotaRow]:
    now = time.time() if now is None else now
    seen = observed or {}
    return [_claude_row(result, sessions, source, seen.get(result.account), now, include_fable) for result in results]


def _claude_row(
    result, sessions: dict[str, int], source: str, seen: float | None, now: float, include_fable: bool
) -> QuotaRow:
    row = QuotaRow(
        agent="claude",
        account=result.account,
        state=result.state,
        sessions=sessions.get(result.account, 0),
        five_hour_left=result.five_hour.remaining,
        seven_day_left=result.seven_day.remaining,
        seven_day_resets_at=result.seven_day.resets_at,
        source=source,
        five_hour_resets_at=result.five_hour.resets_at,
        observed_at=seen,
        cap=account_cap(result, now, include_fable) if session_bands.fresh(seen, now) else None,
        routing_left=result.margin,
        fable_left=result.fable.remaining,
        fable_resets_at=result.fable.resets_at,
    )
    if seen is None or session_bands.fresh(seen, now):
        return row
    return replace(row, state=STALE, five_hour_left=None, seven_day_left=None, routing_left=None, fable_left=None)


def _codex_state(
    account: "codex_router.CodexAccount", quota: "CodexQuota | None", now: float, environ: Mapping[str, str] | None
) -> str:
    if not account.signed_in:
        return "SIGNED_OUT"
    if quota is None:
        return "UNKNOWN"
    if quota.expired:
        return quota.state
    if not codex_router.usable(quota, now, environ):
        return STALE
    current = quota if session_bands.fresh(quota.observed_at, now) else replace(quota, five_hour=QuotaWindow())
    return current.state_at(now)


def _codex_source(account: "codex_router.CodexAccount", quota: "CodexQuota | None", now: float) -> str:
    if quota is None:
        return "no session log"
    if quota.expired:
        return EXPIRED_SOURCE.format(
            login=f"agentihooks codex login {account.name}" if account.is_token else "codex login"
        )
    seen = f"session-log {_duration(int(now), int(quota.observed_at))} ago"
    return f"{seen}, {quota.reached}" if quota.reached else seen


def codex_rows(
    accounts: list, quotas: dict, sessions: dict[str, int], now: float, environ: Mapping[str, str] | None = None
) -> list[QuotaRow]:
    rows = []
    for account in accounts:
        quota = quotas.get(account.name)
        known = quota is not None and not quota.expired
        fresh = known and session_bands.fresh(quota.observed_at, now)
        week = session_bands.left(quota.seven_day.used, quota.seven_day.resets_at, now) if known else None
        cap = codex_router.account_cap(quota, now, environ) if account.signed_in and known else None
        rows.append(
            QuotaRow(
                agent="codex",
                account=account.name,
                state=_codex_state(account, quota, now, environ),
                sessions=sessions.get(account.name, 0),
                five_hour_left=session_bands.left(quota.five_hour.used, quota.five_hour.resets_at, now)
                if fresh
                else None,
                seven_day_left=week,
                seven_day_resets_at=quota.seven_day.resets_at if quota else None,
                source=_codex_source(account, quota, now),
                five_hour_resets_at=quota.five_hour.resets_at if quota else None,
                observed_at=quota.observed_at if quota else None,
                cap=cap,
                routing_left=week if account.signed_in else None,
            )
        )
    return rows


def next_account(pool: list, quotas: dict, sessions: dict[str, int], now: float, environ: dict[str, str]) -> str:
    """The account open placement would give the next launch, among accounts whose credential is not refused."""
    usable = [account for account in pool if not codex_router.refusal(account, environ)]
    try:
        return codex_router.select(usable, quotas, sessions, now, environ=environ)[0].name
    except codex_router.RoutingError:
        return ""


def mark_next(rows: list[QuotaRow], account: str) -> list[QuotaRow]:
    return [replace(row, selected=True) if account and row.account == account else row for row in rows]


def render(rows: list[QuotaRow], now: int, unlisted: int = 0, fable: bool = False, current: str = NO_CURRENT) -> str:
    columns = [*COLUMNS, *(FABLE_COLUMNS if fable else [])]
    table, rank = [], 0
    for row in _ordered(rows):
        rank += row.kind == SUBSCRIPTION
        cells = _cells(str(rank) if row.kind == SUBSCRIPTION else MISSING, row, now, current)
        fable_cells = [_left(row.fable_left), _reset(row.fable_left, row.fable_resets_at, now)]
        table.append([*cells, *(fable_cells if fable else [])])
    widths = [max(len(cell) for cell in column) for column in zip(columns, *table)]
    lines = [columns, ["-" * width for width in widths], *table]
    text = "\n".join("  ".join(cell.ljust(width) for cell, width in zip(line, widths)).rstrip() for line in lines)
    return f"{text}\n{UNLISTED.format(count=unlisted)}" if unlisted > 0 else text


def _ordered(rows: list[QuotaRow]) -> list[QuotaRow]:
    def key(row: QuotaRow) -> tuple:
        harness = HARNESSES.index(row.agent) if row.agent in HARNESSES else len(HARNESSES)
        return harness, row.kind != SUBSCRIPTION, row.routing_left is None, -(row.routing_left or 0)

    return sorted(rows, key=key)


def _left(value: float | None) -> str:
    return MISSING if value is None else f"{value:.0f}%"


def _reset(left: float | None, resets_at: int | None, now: int) -> str:
    return MISSING if left is None or resets_at is None or resets_at <= now else _duration(resets_at, now)


def _cells(rank: str, row: QuotaRow, now: int, current: str) -> list[str]:
    cap = MISSING if row.cap is None else _cap_text(row.cap)
    mark = CURRENT_MARK if current and row.agent == "claude" and row.account == current else ""
    return [
        rank,
        row.agent,
        " ".join(part for part in (row.account, mark, row.master, NEXT_MARK if row.selected else "") if part),
        row.kind,
        row.state,
        f"{row.sessions}/{cap}",
        _weight_text(row.weight),
        cap,
        _left(row.routing_left),
        _left(row.five_hour_left),
        _reset(row.five_hour_left, row.five_hour_resets_at, now),
        _left(row.seven_day_left),
        _reset(row.seven_day_left, row.seven_day_resets_at, now),
    ]


def unlisted_sessions(rows: list[QuotaRow], sessions: Mapping[str, Mapping[str, int]]) -> int:
    listed = {(row.agent, row.account) for row in rows}
    return sum(
        count for harness, live in sessions.items() for name, count in live.items() if (harness, name) not in listed
    )


def api_row(slot: Slot) -> QuotaRow:
    return QuotaRow(
        agent=slot.harness,
        account=slot.account,
        state=OPEN,
        sessions=slot.sessions,
        five_hour_left=None,
        seven_day_left=None,
        seven_day_resets_at=None,
        source=slot.provider,
        cap=slot.cap,
        kind=API,
        weight=slot.weight,
    )


def api_rows(source: "SlotSource", harness: str, now: float) -> list[QuotaRow]:
    from scripts.routing import place

    slots, weight = place.api_side(source, harness, os.environ, now)
    return [api_row(replace(slot, weight=weight)) for slot in slots]


def _masters(harness: str = "") -> dict[str, "MasterAccount"]:
    from scripts.routing import master_account

    return {name: master for name, master in master_account.load(os.environ).items() if harness in ("", name)}


def _interactive_row(master: "MasterAccount", sessions: dict[str, int]) -> QuotaRow:
    return QuotaRow(
        agent=master.harness,
        account=master.slug,
        state=MASTERS_ONLY,
        sessions=sessions.get(master.slug, 0),
        five_hour_left=None,
        seven_day_left=None,
        seven_day_resets_at=None,
        source="interactive login",
        kind=INTERACTIVE,
        master=master.marker,
    )


def _mark(row: QuotaRow, master: "MasterAccount | None") -> QuotaRow:
    declared = master is not None and row.kind != API and row.account == master.slug
    return replace(row, master=master.marker) if declared else row


def with_masters(rows: list[QuotaRow], masters: dict[str, "MasterAccount"], sessions: dict[str, int]) -> list[QuotaRow]:
    marked = [_mark(row, masters.get(row.agent)) for row in rows]
    claude = masters.get("claude")
    if claude and claude.kind == INTERACTIVE and not any(row.master for row in marked if row.agent == "claude"):
        index = next((i for i, row in enumerate(marked) if row.agent != "claude"), len(marked))
        marked.insert(index, _interactive_row(claude, sessions))
    return marked


def _claude(refresh: bool, timeout: float) -> list[QuotaRow]:
    from hooks.context.account_sessions import sessions_by_account
    from scripts.claude_quota_balancer import cached_observations, collect_results, discover_credentials
    from scripts.install import _load_claude_runtime_env
    from scripts.routing.claude_api import ClaudeApiSource

    _load_claude_runtime_env()
    credentials = discover_credentials(os.environ)
    sessions = sessions_by_account()
    if credentials:
        results, source = collect_results(
            credentials, refresh=refresh, timeout=timeout, claude_bin=shutil.which("claude") or "claude"
        )
    else:
        results, source = [result for _, result in cached_observations()], "cached"
    observed = {result.account: at for at, result in cached_observations()}
    rows = claude_rows(results, sessions, source, observed) + api_rows(ClaudeApiSource(sessions), "claude", time.time())
    return with_masters(rows, _masters("claude"), sessions)


def _codex(now: float) -> list[QuotaRow]:
    from hooks.context.account_sessions import codex_sessions_by_account
    from scripts import codex_router
    from scripts.routing.codex_api import CodexApiSource

    pool = codex_router.CodexAccountSource().pool(os.environ)
    sessions = codex_sessions_by_account()
    quotas = codex_router.quotas(pool, os.environ)
    rows = codex_rows(pool, quotas, sessions, now)
    master = _masters("codex").get("codex")
    marked = [_mark(row, master) for row in rows + api_rows(CodexApiSource(sessions), "codex", now)]
    return mark_next(marked, next_account(pool, quotas, sessions, now, dict(os.environ)))


def _render_live(
    rows: list[QuotaRow], sessions: dict[str, int], now: float, fable: bool = False, current: str = NO_CURRENT
) -> str:
    from hooks.context.account_sessions import codex_sessions_by_account

    live = {"claude": sessions, **({} if fable else {"codex": codex_sessions_by_account()})}
    return render(rows, int(now), unlisted_sessions(rows, live), fable, current)


def tokenless_table(master: "MasterAccount | None", sessions: dict[str, int]) -> str:
    now = time.time()
    claude = [_interactive_row(master, sessions)] if master is not None and master.kind == INTERACTIVE else []
    return _render_live(claude + _codex(now), sessions, now)


def current_table(
    observations: list, probed: list, names: set[str], sessions: dict[str, int], fable: bool, current: str
) -> str:
    now = time.time()
    seen = {result.account: (at, result) for at, result in observations if result.account in names}
    seen.update({result.account: (now, result) for result in probed})
    observed = {account: at for account, (at, _) in seen.items()}
    results = [result for _, result in seen.values()]
    return balance_table(results, sessions, now, fable, current, observed)


def balance_table(
    results: list,
    sessions: dict[str, int],
    now: float,
    fable: bool = False,
    current: str = NO_CURRENT,
    observed: dict[str, float] | None = None,
) -> str:
    from scripts.routing.claude_api import ClaudeApiSource

    observed = {result.account: now for result in results} if observed is None else observed
    claude = claude_rows(results, sessions, observed=observed, now=now, include_fable=fable)
    claude = with_masters(claude + api_rows(ClaudeApiSource(sessions), "claude", now), _masters("claude"), sessions)
    return _render_live(claude + ([] if fable else _codex(now)), sessions, now, fable, current)


def _page_quota(now: float) -> dict:
    from hooks.context import account_sessions
    from scripts import claude_quota_balancer, codex_router
    from scripts.routing.claude_api import ClaudeApiSource
    from scripts.routing.codex_api import CodexApiSource

    cached = claude_quota_balancer.cached_observations()
    observed = {result.account: at for at, result in cached}
    pool = codex_router.CodexAccountSource().pool(os.environ)
    live = account_sessions.sessions_by_account()
    codex_live = account_sessions.codex_sessions_by_account()
    claude = claude_rows([result for _, result in cached], live, "cached", observed, now)
    claude += api_rows(ClaudeApiSource(live), "claude", now)
    rows = claude + codex_rows(pool, codex_router.quotas(pool, os.environ), codex_live, now)
    rows += api_rows(CodexApiSource(codex_live), "codex", now)
    rows = with_masters(rows, _masters(), live)
    return {"probed_at": max(observed.values(), default=None), "rows": [asdict(row) for row in rows]}


def page_quota(now: float | None = None) -> dict:
    now = time.time() if now is None else now
    if not _page_cache or now - _page_cache["at"] >= PAGE_TTL_S:
        _page_cache.update(at=now, quota=_page_quota(now))
    return _page_cache["quota"]


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
    from hooks.context.account_sessions import sessions_by_account

    now = time.time()
    rows = _claude(args.refresh, args.timeout) + _codex(now)
    if args.json:
        print(json.dumps([{**asdict(row), "harness": row.agent} for row in rows], indent=2))
    elif rows:
        print(_render_live(rows, sessions_by_account(), now))
    else:
        print("agentihooks quota: no Claude account and no Codex session log found", file=sys.stderr)
    return 0 if rows else 1
