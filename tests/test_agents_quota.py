import json
import os

from scripts import agents_quota, codex_quota
from scripts.claude_quota_balancer import ProbeResult, QuotaWindow


def _event(ts: str, primary: dict | None, secondary: dict | None = None, plan: str = "pro") -> str:
    limits = {"limit_id": "codex", "primary": primary, "secondary": secondary, "plan_type": plan}
    return json.dumps({"timestamp": ts, "type": "event_msg", "payload": {"type": "token_count", "rate_limits": limits}})


def _rollout(home, day: str, name: str, lines: list[str], mtime: float):
    path = home / ".codex" / "sessions" / "2026" / "10" / day / f"rollout-{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    os.utime(path, (mtime, mtime))
    return path


WEEK = {"used_percent": 46.0, "window_minutes": 10080, "resets_at": 1791611551}


def test_a_weekly_only_event_fills_the_seven_day_window():
    quota = codex_quota.parse_event(_event("2026-10-04T15:00:14.258Z", WEEK))
    assert quota.seven_day == QuotaWindow(used=46.0, resets_at=1791611551)
    assert quota.five_hour == QuotaWindow()
    assert quota.plan_type == "pro"


def test_windows_map_by_length_not_by_slot():
    five = {"used_percent": 12.0, "window_minutes": 300, "resets_at": 1}
    quota = codex_quota.parse_event(_event("2026-10-04T15:00:00Z", WEEK, five))
    assert quota.five_hour.used == 12.0
    assert quota.seven_day.used == 46.0


def test_an_event_without_rate_limits_is_skipped():
    assert codex_quota.parse_event(json.dumps({"timestamp": "2026-10-04T15:00:00Z", "payload": {"type": "x"}})) is None
    assert codex_quota.parse_event(_event("2026-10-04T15:00:00Z", None)) is None


def test_the_newest_observation_wins_across_rollouts(tmp_path):
    older = _event("2026-10-04T10:00:00Z", {**WEEK, "used_percent": 40.0})
    newer = _event("2026-10-04T15:00:00Z", {**WEEK, "used_percent": 46.0})
    _rollout(tmp_path, "03", "a", [newer, json.dumps({"payload": {"type": "agent_message"}})], mtime=100)
    _rollout(tmp_path, "04", "b", [older], mtime=200)
    quota = codex_quota.latest_codex_quota({"HOME": str(tmp_path)})
    assert quota.seven_day.used == 46.0


def test_no_session_log_means_no_codex_quota(tmp_path):
    assert codex_quota.latest_codex_quota({"HOME": str(tmp_path)}) is None


def test_codex_state_follows_its_most_used_window():
    assert codex_quota.parse_event(_event("2026-10-04T15:00:00Z", WEEK)).state == "NORMAL"
    assert codex_quota.parse_event(_event("2026-10-04T15:00:00Z", {**WEEK, "used_percent": 92.0})).state == "DRAIN"


def test_rows_list_every_claude_account_and_codex():
    claude = ProbeResult(
        account="ncgma",
        provider_status="allowed",
        state="NORMAL",
        margin=60.0,
        five_hour=QuotaWindow(used=5.0, resets_at=2000),
        seven_day=QuotaWindow(used=40.0, resets_at=9000),
    )
    rows = agents_quota.claude_rows([claude], {"ncgma": 2}, "cached")
    quota = codex_quota.parse_event(_event("2026-10-04T15:00:00Z", WEEK))
    rows.append(agents_quota.codex_row(quota, sessions=1, now=quota.observed_at + 120))
    table = agents_quota.render(rows, now=1000).splitlines()
    assert table[0].split() == [
        "AGENT",
        "ACCOUNT",
        "STATE",
        "SESSIONS",
        "5H",
        "LEFT",
        "7D",
        "LEFT",
        "7D",
        "RESET",
        "SOURCE",
    ]
    assert table[1].split()[:6] == ["claude", "ncgma", "NORMAL", "2", "95%", "60%"]
    assert table[2].split()[:6] == ["codex", "pro", "NORMAL", "1", "?", "54%"]
    assert table[2].endswith("session-log 2m ago")


def test_a_plan_with_only_a_five_hour_window_is_classified_on_it():
    five = {"used_percent": 85.0, "window_minutes": 300, "resets_at": 1}
    quota = codex_quota.parse_event(_event("2026-10-04T15:00:00Z", five))
    assert quota.seven_day.used is None
    assert quota.state == "DRAIN_SOON"


def test_quota_json_lists_every_row(monkeypatch, capsys):
    row = agents_quota.QuotaRow("claude", "ncgma", "NORMAL", 1, 90.0, 40.0, 9000, "cached")
    monkeypatch.setattr(agents_quota, "_claude", lambda refresh, timeout: [row])
    monkeypatch.setattr(agents_quota, "latest_codex_quota", lambda: None)
    monkeypatch.setattr(agents_quota, "_codex_sessions", lambda: 0)
    assert agents_quota.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [
        {
            "agent": "claude",
            "account": "ncgma",
            "state": "NORMAL",
            "sessions": 1,
            "five_hour_left": 90.0,
            "seven_day_left": 40.0,
            "seven_day_resets_at": 9000,
            "source": "cached",
        }
    ]


def test_no_agent_rows_exit_one(monkeypatch, capsys):
    monkeypatch.setattr(agents_quota, "_claude", lambda refresh, timeout: [])
    monkeypatch.setattr(agents_quota, "latest_codex_quota", lambda: None)
    monkeypatch.setattr(agents_quota, "_codex_sessions", lambda: 0)
    assert agents_quota.main([]) == 1
    assert "no Claude account and no Codex session log" in capsys.readouterr().err
