import argparse
import json
import os
import shutil
import sys
import time
from dataclasses import asdict, dataclass

from scripts.claude_quota_balancer import _duration, _percent, _span
from scripts.codex_quota import latest_codex_quota


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


def _left(used: float | None) -> float | None:
    return None if used is None else max(0.0, 100.0 - used)


def claude_rows(results: list, sessions: dict[str, int], source: str) -> list[QuotaRow]:
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
        )
        for result in results
    ]


def codex_row(quota, sessions: int, now: float) -> QuotaRow | None:
    if quota is None:
        return None
    age = int(now - quota.observed_at)
    return QuotaRow(
        agent="codex",
        account=quota.plan_type,
        state=quota.state,
        sessions=sessions,
        five_hour_left=_left(quota.five_hour.used),
        seven_day_left=_left(quota.seven_day.used),
        seven_day_resets_at=quota.seven_day.resets_at,
        source=f"session-log {_span(max(0, age))} ago",
    )


def render(rows: list[QuotaRow], now: int) -> str:
    headers = ["AGENT", "ACCOUNT", "STATE", "SESSIONS", "5H LEFT", "7D LEFT", "7D RESET", "SOURCE"]
    table = [
        [
            row.agent,
            row.account,
            row.state,
            str(row.sessions),
            _percent(row.five_hour_left),
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


def _codex_sessions() -> int:
    from scripts.terminate_agent import sessions

    return len({session.process.pid for session in sessions() if session.target == "codex"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentihooks quota", description="Quota left for every agent harness")
    parser.add_argument("--json", action="store_true", help="Print rows as JSON")
    parser.add_argument("--refresh", action="store_true", help="Probe Claude accounts instead of using the cache")
    parser.add_argument("--timeout", type=float, default=60.0, help="Seconds per Claude account probe")
    args = parser.parse_args(argv)
    now = time.time()
    rows = _claude(args.refresh, args.timeout)
    codex = codex_row(latest_codex_quota(), _codex_sessions(), now)
    rows += [codex] if codex else []
    if args.json:
        print(json.dumps([asdict(row) for row in rows], indent=2))
    elif rows:
        print(render(rows, int(now)))
    else:
        print("agentihooks quota: no Claude account and no Codex session log found", file=sys.stderr)
    return 0 if rows else 1
