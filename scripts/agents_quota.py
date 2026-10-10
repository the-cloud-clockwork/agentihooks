import argparse
import json
import os
import shutil
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING

from scripts import codex_router, session_bands
from scripts.claude_quota_balancer import (
    MASTERS_ONLY,
    NOT_APPLICABLE,
    OPEN,
    RowMarks,
    _cap_text,
    _duration,
    _percent,
    _span,
    _weight_text,
    account_cap,
    render_table,
)
from scripts.routing.slots import API, INTERACTIVE, SUBSCRIPTION, Slot

if TYPE_CHECKING:
    from scripts.codex_quota import CodexQuota
    from scripts.routing.master_account import MasterAccount
    from scripts.routing.slots import SlotSource

PAGE_TTL_S = 60
STALE = "STALE"
NEXT_MARK = "(next)"
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


def claude_rows(
    results: list,
    sessions: dict[str, int],
    source: str,
    observed: dict[str, float] | None = None,
    now: float | None = None,
) -> list[QuotaRow]:
    now = time.time() if now is None else now
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
            cap=account_cap(result, now) if session_bands.fresh((observed or {}).get(result.account), now) else None,
        )
        for result in results
    ]


def _codex_state(account: "codex_router.CodexAccount", quota: "CodexQuota | None", now: float) -> str:
    if not account.signed_in:
        return "SIGNED_OUT"
    if quota is None:
        return "UNKNOWN"
    return quota.state if session_bands.fresh(quota.observed_at, now) else STALE


def _codex_source(quota: "CodexQuota | None", now: float) -> str:
    if quota is None:
        return "no session log"
    seen = f"session-log {_span(max(0, int(now - quota.observed_at)))} ago"
    return f"{seen}, {quota.reached}" if quota.reached else seen


def codex_rows(accounts: list, quotas: dict, sessions: dict[str, int], now: float) -> list[QuotaRow]:
    rows = []
    for account in accounts:
        quota = quotas.get(account.name)
        rows.append(
            QuotaRow(
                agent="codex",
                account=account.name,
                state=_codex_state(account, quota, now),
                sessions=sessions.get(account.name, 0),
                five_hour_left=session_bands.left(quota.five_hour.used, quota.five_hour.resets_at, now)
                if quota
                else None,
                seven_day_left=session_bands.left(quota.seven_day.used, quota.seven_day.resets_at, now)
                if quota
                else None,
                seven_day_resets_at=quota.seven_day.resets_at if quota else None,
                source=_codex_source(quota, now),
                five_hour_resets_at=quota.five_hour.resets_at if quota else None,
                observed_at=quota.observed_at if quota else None,
                cap=codex_router.account_cap(quota, now) if account.signed_in else None,
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


def render(rows: list[QuotaRow], now: int) -> str:
    headers = [
        "AGENT",
        "ACCOUNT",
        "KIND",
        "STATE",
        "SESSIONS",
        "WEIGHT",
        "CAP",
        "5H LEFT",
        "5H RESET",
        "7D LEFT",
        "7D RESET",
        "SOURCE",
    ]
    table = [
        [
            row.agent,
            " ".join(part for part in (row.account, row.master, NEXT_MARK if row.selected else "") if part),
            row.kind,
            row.state,
            f"{row.sessions}/{_cap_text(row.cap)}",
            _weight_text(row.weight),
            _cap_text(row.cap),
            *(
                [NOT_APPLICABLE] * 4
                if row.kind == API
                else [
                    _percent(row.five_hour_left),
                    _duration(row.five_hour_resets_at, now),
                    _percent(row.seven_day_left),
                    _duration(row.seven_day_resets_at, now),
                ]
            ),
            row.source,
        ]
        for row in rows
    ]
    widths = [max(len(cell) for cell in column) for column in zip(headers, *table)]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(line, widths)).rstrip() for line in [headers, *table]
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

    pool = codex_router.accounts(os.environ)
    sessions = codex_sessions_by_account()
    quotas = codex_router.quotas(pool, os.environ)
    rows = codex_rows(pool, quotas, sessions, now)
    master = _masters("codex").get("codex")
    marked = [_mark(row, master) for row in rows + api_rows(CodexApiSource(sessions), "codex", now)]
    return mark_next(marked, next_account(pool, quotas, sessions, now, dict(os.environ)))


def codex_table() -> str:
    now = time.time()
    return render(_codex(now), int(now))


def tokenless_table(master: "MasterAccount | None", sessions: dict[str, int]) -> str:
    table = codex_table()
    if master is None or master.kind != INTERACTIVE:
        return table
    return f"{render_table([], sessions=sessions, marks=RowMarks(master=master))}\n{table}"


def _page_quota(now: float) -> dict:
    from hooks.context import account_sessions
    from scripts import claude_quota_balancer, codex_router
    from scripts.routing.claude_api import ClaudeApiSource
    from scripts.routing.codex_api import CodexApiSource

    cached = claude_quota_balancer.cached_observations()
    observed = {result.account: at for at, result in cached}
    pool = codex_router.routing_pool(os.environ)
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
    now = time.time()
    rows = _claude(args.refresh, args.timeout) + _codex(now)
    if args.json:
        print(json.dumps([asdict(row) for row in rows], indent=2))
    elif rows:
        print(render(rows, int(now)))
    else:
        print("agentihooks quota: no Claude account and no Codex session log found", file=sys.stderr)
    return 0 if rows else 1
