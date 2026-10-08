import json
import os

from scripts import agents_quota, codex_quota
from scripts.claude_quota_balancer import ProbeResult, QuotaWindow
from scripts.codex_router import CodexAccount


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
    rows = agents_quota.claude_rows([claude], {"ncgma": 2}, "cached", {"ncgma": 1000.0}, now=1000)
    quota = codex_quota.parse_event(_event("2026-10-04T15:00:00Z", WEEK))
    accounts = [CodexAccount("default"), CodexAccount("alpha", "AH_CX_TOKEN_alpha")]
    rows += agents_quota.codex_rows(accounts, {"default": quota}, {"default": 1}, now=quota.observed_at + 120)
    table = agents_quota.render(rows, now=1000).splitlines()
    assert table[0].split() == [
        "AGENT",
        "ACCOUNT",
        "STATE",
        "SESSIONS",
        "5H",
        "LEFT",
        "5H",
        "RESET",
        "7D",
        "LEFT",
        "7D",
        "RESET",
        "SOURCE",
    ]
    assert table[1].split()[:8] == ["claude", "ncgma", "NORMAL", "2/6", "95%", "16m", "60%", "2h13m"]
    assert table[2].split()[:6] == ["codex", "default", "NORMAL", "1/6", "?", "?"]
    assert table[2].endswith("session-log 2m ago")
    assert table[3].split() == ["codex", "alpha", "UNKNOWN", "0/?", "?", "?", "?", "?", "no", "session", "log"]


def test_a_claude_row_with_no_or_a_stale_reading_time_shows_no_cap():
    claude = ProbeResult("ncgma", "allowed", "NORMAL", 60.0, QuotaWindow(5.0, 2000), QuotaWindow(40.0, 9000))
    assert agents_quota.claude_rows([claude], {}, "cached", now=1000)[0].cap is None
    assert agents_quota.claude_rows([claude], {}, "cached", {"ncgma": 99.0}, now=1000)[0].cap is None
    assert agents_quota.claude_rows([claude], {}, "cached", {"ncgma": 100.0}, now=1000)[0].cap == 6


def test_rows_carry_both_reset_times_and_when_each_was_observed():
    claude = ProbeResult("ncgma", "allowed", "NORMAL", 60.0, QuotaWindow(5.0, 2000), QuotaWindow(40.0, 9000))
    [row] = agents_quota.claude_rows([claude], {}, "cached", {"ncgma": 1500.0})
    assert (row.five_hour_resets_at, row.seven_day_resets_at, row.observed_at) == (2000, 9000, 1500.0)
    five = {"used_percent": 10.0, "window_minutes": 300, "resets_at": 1791600000}
    quota = codex_quota.parse_event(_event("2026-10-04T15:00:00Z", five, WEEK))
    [codex] = agents_quota.codex_rows([CodexAccount("default")], {"default": quota}, {}, now=quota.observed_at)
    assert (codex.five_hour_resets_at, codex.seven_day_resets_at) == (1791600000, WEEK["resets_at"])
    assert codex.observed_at == quota.observed_at


def test_a_signed_out_default_login_is_listed_as_signed_out():
    rows = agents_quota.codex_rows([CodexAccount("default", signed_in=False)], {}, {}, now=0)
    assert [(row.account, row.state, row.sessions) for row in rows] == [("default", "SIGNED_OUT", 0)]


def test_each_codex_account_reads_only_its_own_session_logs(tmp_path, monkeypatch):
    from scripts import codex_router

    ids = {"a": "11111111-1111-1111-1111-111111111111", "b": "22222222-2222-2222-2222-222222222222"}
    ids["c"] = "33333333-3333-3333-3333-333333333333"
    _rollout(
        tmp_path,
        "04",
        f"2026-10-04T10-00-00-{ids['a']}",
        [_event("2026-10-04T10:00:00Z", {**WEEK, "used_percent": 10.0})],
        300,
    )
    _rollout(
        tmp_path,
        "04",
        f"2026-10-04T09-00-00-{ids['b']}",
        [_event("2026-10-04T09:00:00Z", {**WEEK, "used_percent": 20.0})],
        200,
    )
    _rollout(
        tmp_path,
        "04",
        f"2026-10-04T08-00-00-{ids['c']}",
        [_event("2026-10-04T08:00:00Z", {**WEEK, "used_percent": 30.0})],
        100,
    )
    registry = {ids["a"]: {"account": "alpha"}, ids["b"]: {"account": "unrouted"}}
    monkeypatch.setattr(codex_router, "_registry", lambda: registry)
    accounts = [
        CodexAccount("default"),
        CodexAccount("alpha", "AH_CX_TOKEN_alpha"),
        CodexAccount("beta", "AH_CX_TOKEN_beta"),
    ]
    quotas = codex_router.quotas(accounts, {"HOME": str(tmp_path)})
    assert quotas["alpha"].seven_day.used == 10.0
    assert quotas["default"].seven_day.used == 20.0
    assert quotas["beta"] is None


def test_a_plan_with_only_a_five_hour_window_is_classified_on_it():
    five = {"used_percent": 85.0, "window_minutes": 300, "resets_at": 1}
    quota = codex_quota.parse_event(_event("2026-10-04T15:00:00Z", five))
    assert quota.seven_day.used is None
    assert quota.state == "DRAIN_SOON"


def test_quota_json_lists_every_row(monkeypatch, capsys):
    row = agents_quota.QuotaRow("claude", "ncgma", "NORMAL", 1, 90.0, 40.0, 9000, "cached")
    monkeypatch.setattr(agents_quota, "_claude", lambda refresh, timeout: [row])
    monkeypatch.setattr(agents_quota, "_codex", lambda now: [])
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
            "five_hour_resets_at": None,
            "observed_at": None,
            "cap": None,
        }
    ]


def test_no_agent_rows_exit_one(monkeypatch, capsys):
    monkeypatch.setattr(agents_quota, "_claude", lambda refresh, timeout: [])
    monkeypatch.setattr(agents_quota, "_codex", lambda now: [])
    assert agents_quota.main([]) == 1
    assert "no Claude account and no Codex session log" in capsys.readouterr().err


def test_without_registry_entries_the_default_login_keeps_every_session_log(tmp_path, monkeypatch):
    from scripts import codex_router

    session = "44444444-4444-4444-4444-444444444444"
    _rollout(tmp_path, "04", f"2026-10-04T10-00-00-{session}", [_event("2026-10-04T10:00:00Z", WEEK)], 100)
    monkeypatch.setattr(codex_router, "_registry", lambda: {})
    quotas = codex_router.quotas([CodexAccount("default")], {"HOME": str(tmp_path)})
    assert quotas["default"] == codex_quota.latest_codex_quota({"HOME": str(tmp_path)})
    assert quotas["default"].seven_day.used == 46.0


def test_page_quota_reads_the_balance_cache_and_codex_logs_without_probing(monkeypatch):
    from hooks.context import account_sessions
    from scripts import claude_quota_balancer, codex_router

    agents_quota._page_cache.clear()
    claude = ProbeResult("tccgma", "ok", "OK", 78.0, QuotaWindow(used=8.0), QuotaWindow(used=22.0))
    monkeypatch.setattr(claude_quota_balancer, "cached_observations", lambda: [(1.0, claude)])
    monkeypatch.setattr(claude_quota_balancer, "collect_results", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    monkeypatch.setattr(account_sessions, "sessions_by_account", lambda: {"tccgma": 2})
    monkeypatch.setattr(account_sessions, "codex_sessions_by_account", lambda: {"default": 1})
    monkeypatch.setattr(codex_router, "routing_pool", lambda environ: [CodexAccount("default")])
    monkeypatch.setattr(codex_router, "quotas", lambda pool, environ: {"default": None})
    quota = agents_quota.page_quota(now=100.0)
    assert [(r["agent"], r["account"], r["sessions"], r["cap"]) for r in quota["rows"]] == [
        ("claude", "tccgma", 2, 6),
        ("codex", "default", 1, None),
    ]
    assert quota["rows"][0]["five_hour_left"] == 92.0
    assert quota["rows"][0]["seven_day_left"] == 78.0
    assert quota["rows"][1]["five_hour_left"] is None
    assert quota["rows"][0]["observed_at"] == 1.0
    assert quota["probed_at"] == 1.0


def test_page_quota_refresh_probes_once_a_minute_and_drops_the_page_cache(monkeypatch):
    agents_quota._page_cache.clear()
    agents_quota._last_refresh.clear()
    probes = []
    monkeypatch.setattr(
        agents_quota, "_page_quota", lambda now: {"cap": 3, "rows": [{"agent": "claude", "account": "a", "at": now}]}
    )
    agents_quota.page_quota(now=1000.0)
    assert agents_quota.refresh_page_quota(lambda: probes.append(1) or "", now=1010.0) == ""
    assert agents_quota.page_quota(now=1011.0)["rows"][0]["at"] == 1011.0
    assert agents_quota.refresh_page_quota(lambda: probes.append(2) or "", now=1069.0) == ""
    assert probes == [1]
    assert agents_quota.refresh_page_quota(lambda: probes.append(3) or "", now=1070.0) == ""
    assert probes == [1, 3]


def test_a_failed_refresh_keeps_the_cache_and_retries_on_the_next_call(monkeypatch):
    agents_quota._page_cache.clear()
    agents_quota._last_refresh.clear()
    monkeypatch.setattr(
        agents_quota, "_page_quota", lambda now: {"cap": 3, "rows": [{"agent": "claude", "account": "a", "at": now}]}
    )
    agents_quota.page_quota(now=1000.0)
    assert agents_quota.refresh_page_quota(lambda: "probe timed out", now=1010.0) == "probe timed out"
    assert agents_quota.page_quota(now=1011.0)["rows"][0]["at"] == 1000.0
    assert agents_quota.refresh_page_quota(lambda: "", now=1011.0) == ""


def test_page_quota_routes_the_environment_and_names_each_source(monkeypatch):
    from hooks.context import account_sessions
    from scripts import claude_quota_balancer, codex_router

    agents_quota._page_cache.clear()
    claude = ProbeResult("tccgma", "ok", "OK", 78.0, QuotaWindow(used=8.0), QuotaWindow(used=22.0))
    quota = codex_quota.parse_event(_event("2026-10-04T15:00:00Z", WEEK))
    pool = [CodexAccount("default")]
    seen = []
    monkeypatch.setattr(claude_quota_balancer, "cached_observations", lambda: [(1.0, claude)])
    monkeypatch.setattr(account_sessions, "sessions_by_account", lambda: {})
    monkeypatch.setattr(account_sessions, "codex_sessions_by_account", lambda: {})
    monkeypatch.setattr(codex_router, "routing_pool", lambda environ: seen.append(environ) or pool)
    monkeypatch.setattr(codex_router, "quotas", lambda p, environ: seen.append((p, environ)) or {"default": quota})
    rows = agents_quota.page_quota(now=quota.observed_at + 120)["rows"]
    assert seen == [os.environ, (pool, os.environ)]
    assert [r["source"] for r in rows] == ["cached", "session-log 2m ago"]
    assert rows[1]["seven_day_left"] == 54.0


def test_page_quota_is_reused_for_a_minute(monkeypatch):
    calls = []
    agents_quota._page_cache.clear()
    monkeypatch.setattr(
        agents_quota,
        "_page_quota",
        lambda now: calls.append(now) or {"cap": 3, "rows": [{"agent": "claude", "account": "a", "at": now}]},
    )
    assert agents_quota.page_quota(now=1000.0)["rows"][0]["at"] == 1000.0
    assert agents_quota.page_quota(now=1059.0)["rows"][0]["at"] == 1000.0
    assert agents_quota.page_quota(now=1060.0)["rows"][0]["at"] == 1060.0
    assert calls == [1000.0, 1060.0]


def test_a_claude_cap_reads_the_five_hour_window_at_the_given_time():
    claude = ProbeResult("ncgma", "allowed", "NORMAL", 5.0, QuotaWindow(95.0, 2000), QuotaWindow(10.0, 9000))
    [row] = agents_quota.claude_rows([claude], {}, "cached", {"ncgma": 1000.0}, now=1000)
    assert row.cap == 2


def test_the_cached_claude_table_carries_sessions_source_and_reading_time(monkeypatch):
    import time

    from hooks.context import account_sessions
    from scripts import claude_quota_balancer, install

    at = time.time()
    claude = ProbeResult("ncgma", "allowed", "NORMAL", 60.0, QuotaWindow(5.0), QuotaWindow(10.0))
    monkeypatch.setattr(install, "_load_claude_runtime_env", lambda: None)
    monkeypatch.setattr(claude_quota_balancer, "discover_credentials", lambda environ: [])
    monkeypatch.setattr(claude_quota_balancer, "cached_observations", lambda: [(at, claude)])
    monkeypatch.setattr(account_sessions, "sessions_by_account", lambda: {"ncgma": 2})
    [row] = agents_quota._claude(False, 1.0)
    assert (row.account, row.sessions, row.source, row.observed_at, row.cap) == ("ncgma", 2, "cached", at, 6)


def test_the_codex_table_renders_at_the_current_time(monkeypatch):
    seen = []
    row = agents_quota.QuotaRow("codex", "default", "NORMAL", 0, 90.0, 80.0, 1_000_000 + 7200, "log", cap=6)
    monkeypatch.setattr(agents_quota.time, "time", lambda: 1_000_000.5)
    monkeypatch.setattr(agents_quota, "_codex", lambda now: seen.append(now) or [row])
    table = agents_quota.codex_table()
    assert seen == [1_000_000.5]
    assert "2h00m" in table.splitlines()[1]


def test_page_quota_without_a_cached_reading_has_no_probe_time(monkeypatch):
    from hooks.context import account_sessions
    from scripts import claude_quota_balancer, codex_router

    agents_quota._page_cache.clear()
    monkeypatch.setattr(claude_quota_balancer, "cached_observations", lambda: [])
    monkeypatch.setattr(account_sessions, "sessions_by_account", lambda: {})
    monkeypatch.setattr(account_sessions, "codex_sessions_by_account", lambda: {})
    monkeypatch.setattr(codex_router, "routing_pool", lambda environ: [])
    monkeypatch.setattr(codex_router, "quotas", lambda pool, environ: {})
    assert agents_quota.page_quota(now=100.0) == {"probed_at": None, "rows": []}
