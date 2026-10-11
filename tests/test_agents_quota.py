import json
import os

import pytest

from scripts import agents_quota, codex_quota
from scripts.claude_quota_balancer import ProbeResult, QuotaWindow
from scripts.codex_router import CodexAccount
from scripts.routing.slots import API, API_UNBOUNDED, INTERACTIVE, SUBSCRIPTION, Slot


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
HEADER = "# HARNESS ACCOUNT KIND STATE SESSIONS WEIGHT CAP ROUTING LEFT 5H LEFT 5H RESET 7D LEFT 7D RESET".split()


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


@pytest.mark.parametrize("content", ["", '{"payload": {"type": "x"}}\n', '{"rate_limits": null}\n'])
def test_a_rollout_without_a_quota_event_has_no_reading(content, tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text(content)
    assert codex_quota._last_in(path) is None


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
    assert table[0].split() == HEADER
    assert table[2].split() == [
        "1",
        "claude",
        "ncgma",
        "subscription",
        "NORMAL",
        "2/6",
        "-",
        "6",
        "60%",
        "95%",
        "16m",
        "60%",
        "2h13m",
    ]
    default = table[3].split()
    assert default[:8] == ["2", "codex", "default", "subscription", "NORMAL", "1/6", "-", "6"]
    assert default[9:11] == ["-", "-"] and default[8] == default[11]
    assert table[4].split() == ["3", "codex", "alpha", "subscription", "UNKNOWN", "0/-", *["-"] * 7]
    assert "?" not in "\n".join(table) and "SOURCE" not in table[0]


def test_an_api_row_is_open_with_its_weight_cap_and_provider():
    slot = Slot("codex", "api", 4, 1, kind=API, weight=30, provider="openai")
    unbounded = Slot("claude", "api", API_UNBOUNDED, 2, kind=API, weight=0, provider="gateway")
    rows = [agents_quota.api_row(slot), agents_quota.api_row(unbounded)]
    assert rows[0] == agents_quota.QuotaRow(
        "codex", "api", "OPEN", 1, None, None, None, "openai", cap=4, kind="api", weight=30
    )
    table = agents_quota.render(rows, now=1000).splitlines()
    assert table[2].split() == ["-", "claude", "api", "api", "OPEN", "2/none", "0%", "none", *["-"] * 5]
    assert table[3].split() == ["-", "codex", "api", "api", "OPEN", "1/4", "30%", "4", *["-"] * 5]


def test_api_rows_carry_the_weight_from_the_routing_settings(monkeypatch):
    from scripts.routing import place

    source = object()
    slot = Slot("codex", "api", 3, 1, kind=API, provider="openai")
    calls = []
    monkeypatch.setattr(place, "api_side", lambda *args: calls.append(args) or ([slot], 25))
    [row] = agents_quota.api_rows(source, "codex", 50.0)
    assert (row.kind, row.weight, row.cap, row.sessions, row.source) == ("api", 25, 3, 1, "openai")
    assert calls == [(source, "codex", agents_quota.os.environ, 50.0)]
    monkeypatch.setattr(place, "api_side", lambda *args: ([], 0))
    assert agents_quota.api_rows(source, "codex", 50.0) == []


def test_quota_json_rows_carry_kind_and_weight(monkeypatch, capsys):
    sub = agents_quota.QuotaRow("codex", "default", "NORMAL", 0, 90.0, 80.0, None, "log", cap=6)
    api = agents_quota.api_row(Slot("codex", "api", 4, 1, kind=API, weight=30, provider="openai"))
    monkeypatch.setattr(agents_quota, "_claude", lambda refresh, timeout: [])
    monkeypatch.setattr(agents_quota, "_codex", lambda now: [sub, api])
    assert agents_quota.main(["--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [(row["kind"], row["weight"]) for row in rows] == [("subscription", None), ("api", 30)]


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
            "kind": "subscription",
            "weight": None,
            "master": "",
            "selected": False,
            "routing_left": None,
            "fable_left": None,
            "fable_resets_at": None,
            "harness": "claude",
        }
    ]


def test_every_quota_reader_appends_the_api_row_of_its_harness(monkeypatch):
    from hooks.context import account_sessions
    from scripts import claude_quota_balancer, codex_router, install

    seen = []

    def api_rows(source, harness, now):
        seen.append((type(source).__name__, dict(source.sessions), harness, now))
        return [agents_quota.api_row(Slot(harness, "api", 2, 1, kind=API, weight=10, provider="p"))]

    agents_quota._page_cache.clear()
    monkeypatch.setattr(agents_quota, "api_rows", api_rows)
    monkeypatch.setattr(install, "_load_claude_runtime_env", lambda: None)
    monkeypatch.setattr(claude_quota_balancer, "discover_credentials", lambda environ: [])
    monkeypatch.setattr(claude_quota_balancer, "cached_observations", lambda: [])
    monkeypatch.setattr(account_sessions, "sessions_by_account", lambda: {"api": 1})
    monkeypatch.setattr(account_sessions, "codex_sessions_by_account", lambda: {"api": 3})
    monkeypatch.setattr(codex_router, "accounts", lambda environ: [])
    monkeypatch.setattr(codex_router, "quotas", lambda pool, environ: {})
    monkeypatch.setattr(agents_quota.time, "time", lambda: 42.0)
    codex_nows = []
    monkeypatch.setattr(agents_quota, "codex_rows", lambda pool, quotas, sessions, now: codex_nows.append(now) or [])
    assert [(row.agent, row.kind) for row in agents_quota._claude(False, 1.0)] == [("claude", "api")]
    assert [(row.agent, row.kind) for row in agents_quota._codex(5.0)] == [("codex", "api")]
    rows = agents_quota.page_quota(now=100.0)["rows"]
    assert [(row["agent"], row["kind"], row["weight"]) for row in rows] == [("claude", "api", 10), ("codex", "api", 10)]
    assert seen == [
        ("ClaudeApiSource", {"api": 1}, "claude", 42.0),
        ("CodexApiSource", {"api": 3}, "codex", 5.0),
        ("ClaudeApiSource", {"api": 1}, "claude", 100.0),
        ("CodexApiSource", {"api": 3}, "codex", 100.0),
    ]
    assert codex_nows == [5.0, 100.0]


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
    monkeypatch.setattr(codex_router, "accounts", lambda environ: [CodexAccount("default")])
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


def test_the_declared_masters_mark_their_rows_and_a_tokenless_claude_slug_gets_an_interactive_row():
    from scripts.routing.master_account import MasterAccount

    claude = ProbeResult("tccgma", "ok", "OK", 78.0, QuotaWindow(used=8.0), QuotaWindow(used=22.0))
    rows = agents_quota.claude_rows([claude], {"tccgma": 2, "home": 1}, "cached", {}, now=1000)
    rows += agents_quota.codex_rows([CodexAccount("default")], {}, {}, now=1000)
    masters = {
        "claude": MasterAccount("claude", "home", "max", INTERACTIVE),
        "codex": MasterAccount("codex", "default", "", INTERACTIVE),
    }

    marked = agents_quota.with_masters(rows, masters, {"tccgma": 2, "home": 1})

    assert [(row.agent, row.account, row.kind, row.state, row.sessions, row.master) for row in marked] == [
        ("claude", "tccgma", "subscription", "OK", 2, ""),
        ("claude", "home", "interactive", "MASTERS", 1, "MASTER max"),
        ("codex", "default", "subscription", "UNKNOWN", 0, "MASTER"),
    ]
    table = agents_quota.render(marked, now=1000).splitlines()
    assert table[3].split()[:6] == ["-", "claude", "home", "MASTER", "max", "interactive"]
    assert table[4].split()[:4] == ["2", "codex", "default", "MASTER"]
    subscription = {"claude": MasterAccount("claude", "tccgma", "", SUBSCRIPTION)}
    assert [row.master for row in agents_quota.with_masters(rows, subscription, {})] == ["MASTER", ""]


def test_an_interactive_master_already_listed_is_marked_and_not_listed_twice():
    from scripts.routing.master_account import MasterAccount

    home = ProbeResult("home-login", "ok", "OK", 78.0, QuotaWindow(used=8.0), QuotaWindow(used=22.0))
    rows = agents_quota.claude_rows([home], {}, "cached", {}, now=1000)
    master = MasterAccount("claude", "home-login", "", INTERACTIVE)

    marked = agents_quota.with_masters(rows, {"claude": master}, {})
    assert [(row.account, row.kind, row.master) for row in marked] == [("home-login", "subscription", "MASTER")]
    lone = agents_quota.with_masters([], {"claude": MasterAccount("claude", "away", "", INTERACTIVE)}, {})
    assert [(row.account, row.sessions) for row in lone] == [("away", 0)]
    plain = agents_quota.render(agents_quota.with_masters(rows, {}, {}), now=1000).splitlines()
    assert plain[0].startswith("#  HARNESS  ACCOUNT     KIND")
    assert plain[2].startswith("1  claude   home-login  subscription")


def test_tokenless_table_lists_an_interactive_master_and_the_codex_rows_in_one_table(monkeypatch):
    from hooks.context import account_sessions
    from scripts.routing.master_account import MasterAccount

    seen = []
    row = agents_quota.QuotaRow("codex", "alpha", "NORMAL", 0, None, 80.0, None, "log", cap=6, routing_left=80.0)
    monkeypatch.setattr(agents_quota.time, "time", lambda: 1_000_000.5)
    monkeypatch.setattr(agents_quota, "_codex", lambda now: seen.append(now) or [row])
    monkeypatch.setattr(account_sessions, "codex_sessions_by_account", lambda: {})
    interactive = MasterAccount("claude", "home", "max", INTERACTIVE)

    lines = agents_quota.tokenless_table(interactive, {"home": 2}).splitlines()
    assert seen == [1_000_000.5]
    assert lines[0].split() == HEADER
    assert lines[2].split()[:8] == ["-", "claude", "home", "MASTER", "max", "interactive", "MASTERS", "2/-"]
    assert lines[3].split()[:3] == ["1", "codex", "alpha"]
    assert len(lines) == 4
    alone = agents_quota.tokenless_table(None, {"stray": 3}).splitlines()
    assert [line.split()[1] for line in alone[2:3]] == ["codex"]
    assert alone[-1] == "+3 sessions on unlisted account names"
    subscription = MasterAccount("claude", "luna", "", SUBSCRIPTION)
    assert len(agents_quota.tokenless_table(subscription, {}).splitlines()) == 3


def test_every_quota_reader_marks_the_declared_masters(monkeypatch):
    from hooks.context import account_sessions
    from scripts import claude_quota_balancer, codex_router, install
    from scripts.routing.master_account import MasterAccount

    agents_quota._page_cache.clear()
    masters = {
        "claude": MasterAccount("claude", "home", "", INTERACTIVE),
        "codex": MasterAccount("codex", "default", "pro", INTERACTIVE),
    }
    monkeypatch.setattr(
        agents_quota, "_masters", lambda harness="": {k: v for k, v in masters.items() if harness in ("", k)}
    )
    monkeypatch.setattr(agents_quota, "api_rows", lambda source, harness, now: [])
    monkeypatch.setattr(install, "_load_claude_runtime_env", lambda: None)
    monkeypatch.setattr(claude_quota_balancer, "discover_credentials", lambda environ: [])
    monkeypatch.setattr(claude_quota_balancer, "cached_observations", lambda: [])
    monkeypatch.setattr(account_sessions, "sessions_by_account", lambda: {"home": 1})
    monkeypatch.setattr(account_sessions, "codex_sessions_by_account", lambda: {})
    monkeypatch.setattr(codex_router, "accounts", lambda environ: [CodexAccount("default")])
    monkeypatch.setattr(codex_router, "quotas", lambda pool, environ: {})

    assert [(row.account, row.master) for row in agents_quota._claude(False, 1.0)] == [("home", "MASTER")]
    assert [(row.account, row.master) for row in agents_quota._codex(5.0)] == [("default", "MASTER pro")]
    rows = agents_quota.page_quota(now=100.0)["rows"]
    assert [(row["account"], row["kind"], row["master"]) for row in rows] == [
        ("home", "interactive", "MASTER"),
        ("default", "subscription", "MASTER pro"),
    ]


def test_masters_fall_back_to_the_file_routing_settings_without_redis(monkeypatch, tmp_path):
    from scripts.routing import place
    from scripts.routing.settings import FileSettings

    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    monkeypatch.setattr(place, "_client", lambda environ: None)
    FileSettings(tmp_path / "routing-settings.json").set("master-account-claude", "home", "operator", 1.0)
    assert agents_quota._masters()["claude"].slug == "home"
    assert agents_quota._masters("codex") == {}


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
    monkeypatch.setattr(codex_router, "accounts", lambda environ: seen.append(environ) or pool)
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


def test_one_table_sorts_each_harness_by_routing_left_and_shows_dashes_for_missing_values():
    claude = [
        ProbeResult("low", "allowed", "NORMAL", 40.0, QuotaWindow(10.0, 2000), QuotaWindow(60.0, 9000)),
        ProbeResult("high", "allowed", "NORMAL", 80.0, QuotaWindow(5.0, 2000), QuotaWindow(20.0, 9000)),
    ]
    rows = agents_quota.claude_rows(claude, {"low": 1}, "cached", {"low": 1000.0, "high": 1000.0}, now=1000)
    rows += [agents_quota.api_row(Slot("claude", "api", 3, 0, kind=API, weight=10, provider="gateway"))]
    pro = codex_quota.CodexQuota(1000, "pro", seven_day=QuotaWindow(7.0, 9000))
    expired = codex_quota.CodexQuota(1000, "?", expired=True)
    accounts = [CodexAccount("gone", "AH_CX_TOKEN_gone"), CodexAccount("pro", "AH_CX_TOKEN_pro")]
    rows += agents_quota.codex_rows(accounts, {"pro": pro, "gone": expired}, {}, now=1000)
    table = agents_quota.render(rows, now=1000).splitlines()
    assert [line.split()[:3] for line in table[2:]] == [
        ["1", "claude", "high"],
        ["2", "claude", "low"],
        ["-", "claude", "api"],
        ["3", "codex", "pro"],
        ["4", "codex", "gone"],
    ]
    assert table[5].split() == [
        "3",
        "codex",
        "pro",
        "subscription",
        "NORMAL",
        "0/6",
        "-",
        "6",
        "93%",
        "-",
        "-",
        "93%",
        "2h13m",
    ]
    assert table[6].split() == ["4", "codex", "gone", "subscription", "EXPIRED", "0/-", "-", "-", *["-"] * 5]
    assert "?" not in "\n".join(table)
    assert [row.routing_left for row in rows] == [40.0, 80.0, None, None, 93.0]


def test_codex_routing_left_is_the_week_whenever_the_week_is_known():
    old = codex_quota.CodexQuota(1000, "pro", seven_day=QuotaWindow(30.0, 10**10))
    [row] = agents_quota.codex_rows(
        [CodexAccount("alpha", "AH_CX_TOKEN_alpha")], {"alpha": old}, {}, now=1000 + 7 * 3600
    )
    assert (row.state, row.cap, row.routing_left) == ("STALE", None, 70.0)


def test_current_lists_discovered_accounts_only_and_a_stale_reading_as_stale(monkeypatch):
    from hooks.context import account_sessions

    monkeypatch.setattr(agents_quota.time, "time", lambda: 20_000.0)
    monkeypatch.setattr(agents_quota, "_codex", lambda now: [])
    monkeypatch.setattr(agents_quota, "_masters", lambda harness="": {})
    monkeypatch.setattr(account_sessions, "codex_sessions_by_account", lambda: {})
    old = ProbeResult("alpha", "allowed", "NORMAL", 70.0, QuotaWindow(20.0, 30_000), QuotaWindow(30.0, 90_000))
    gone = ProbeResult("nctcc", "allowed", "NORMAL", 90.0, QuotaWindow(10.0, 30_000), QuotaWindow(10.0, 90_000))
    fresh = ProbeResult("beta", "allowed", "NORMAL", 60.0, QuotaWindow(20.0, 19_000), QuotaWindow(40.0, 90_000))
    observations = [(20_000.0 - 3 * 3600, old), (20_000.0 - 3 * 3600, gone)]
    table = agents_quota.current_table(observations, [fresh], {"alpha", "beta"}, {}, False, "beta").splitlines()
    assert [line.split()[2] for line in table[2:]] == ["beta", "alpha"]
    assert table[2].split()[:11] == [
        "1",
        "claude",
        "beta",
        "(current)",
        "subscription",
        "NORMAL",
        "0/6",
        "-",
        "6",
        "60%",
        "80%",
    ]
    assert table[2].split()[11] == "-"
    assert table[3].split() == ["2", "claude", "alpha", "subscription", "STALE", "0/-", "-", "-", *["-"] * 5]


def test_current_without_a_claude_account_still_lists_the_codex_rows(monkeypatch):
    from hooks.context import account_sessions

    codex = agents_quota.QuotaRow("codex", "personal", "NORMAL", 0, 60.0, 70.0, 9000, "log", cap=6, routing_left=70.0)
    monkeypatch.setattr(agents_quota, "_codex", lambda now: [codex])
    monkeypatch.setattr(agents_quota, "_masters", lambda harness="": {})
    monkeypatch.setattr(account_sessions, "codex_sessions_by_account", lambda: {})
    table = agents_quota.current_table([], [], {"alpha"}, {}, False, "").splitlines()
    assert [line.split()[1:3] for line in table[2:]] == [["codex", "personal"]]


def test_a_signed_out_login_has_no_routing_and_an_expired_one_no_five_hour_window():
    week = codex_quota.CodexQuota(1000, "pro", QuotaWindow(20.0, 2000), QuotaWindow(30.0, 9000))
    expired = codex_quota.CodexQuota(1000, "?", QuotaWindow(20.0, 2000), QuotaWindow(30.0, 9000), expired=True)
    accounts = [CodexAccount("default", signed_in=False), CodexAccount("alpha", "AH_CX_TOKEN_alpha")]
    rows = agents_quota.codex_rows(accounts, {"default": week, "alpha": expired}, {}, now=1000)
    assert [(row.routing_left, row.five_hour_left) for row in rows] == [(None, 80.0), (None, None)]


def test_a_reset_without_its_window_shows_a_dash():
    row = agents_quota.QuotaRow("codex", "alpha", "STALE", 0, None, None, 9000, "log", five_hour_resets_at=2000)
    assert agents_quota.render([row], now=1000).splitlines()[2].split()[-4:] == ["-", "-", "-", "-"]


def test_the_unlisted_sessions_line_shows_only_a_positive_count():
    rows = [agents_quota.QuotaRow("claude", "alpha", "NORMAL", 1, None, None, None, "log")]
    sessions = {"claude": {"alpha": 1, "old-name": 2, "other": 1}, "codex": {"alpha": 4}}
    assert agents_quota.unlisted_sessions(rows, sessions) == 7
    assert agents_quota.unlisted_sessions(rows, {"claude": {"alpha": 3}}) == 0
    assert agents_quota.render(rows, now=0, unlisted=7).splitlines()[-1] == "+7 sessions on unlisted account names"
    assert len(agents_quota.render(rows, now=0, unlisted=0).splitlines()) == 3


def test_quota_prints_one_table_and_json_rows_name_their_harness(monkeypatch, capsys):
    from hooks.context import account_sessions

    row = agents_quota.QuotaRow("codex", "alpha", "NORMAL", 0, None, 80.0, None, "log", cap=6, routing_left=80.0)
    monkeypatch.setattr(agents_quota, "_claude", lambda refresh, timeout: [])
    monkeypatch.setattr(agents_quota, "_codex", lambda now: [row])
    monkeypatch.setattr(account_sessions, "sessions_by_account", lambda: {"stray": 2})
    monkeypatch.setattr(account_sessions, "codex_sessions_by_account", lambda: {})
    assert agents_quota.main([]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == HEADER and lines[2].split()[:3] == ["1", "codex", "alpha"]
    assert lines[-1] == "+2 sessions on unlisted account names"
    assert agents_quota.main(["--json"]) == 0
    [found] = json.loads(capsys.readouterr().out)
    assert (found["harness"], found["account"], found["routing_left"]) == ("codex", "alpha", 80.0)


def test_page_quota_without_a_cached_reading_has_no_probe_time(monkeypatch):
    from hooks.context import account_sessions
    from scripts import claude_quota_balancer, codex_router

    agents_quota._page_cache.clear()
    monkeypatch.setattr(claude_quota_balancer, "cached_observations", lambda: [])
    monkeypatch.setattr(account_sessions, "sessions_by_account", lambda: {})
    monkeypatch.setattr(account_sessions, "codex_sessions_by_account", lambda: {})
    monkeypatch.setattr(codex_router, "accounts", lambda environ: [])
    monkeypatch.setattr(codex_router, "quotas", lambda pool, environ: {})
    assert agents_quota.page_quota(now=100.0) == {"probed_at": None, "rows": []}


def test_codex_quota_rows_drop_the_default_login_once_token_accounts_exist(monkeypatch):
    from hooks.context import account_sessions
    from scripts import codex_router

    monkeypatch.setenv("AH_CX_TOKEN_alpha", "cx-value")
    monkeypatch.setattr(codex_router, "default_signed_in", lambda environ, run=None: pytest.fail("no default row"))
    monkeypatch.setattr(codex_router, "quotas", lambda pool, environ: {})
    monkeypatch.setattr(account_sessions, "codex_sessions_by_account", lambda: {})
    monkeypatch.setattr(agents_quota, "_masters", lambda harness="": {})
    monkeypatch.setattr(agents_quota, "api_rows", lambda source, harness, now: [])
    monkeypatch.setattr(agents_quota, "next_account", lambda *args: "")
    assert [row.account for row in agents_quota._codex(5.0)] == ["alpha"]


def test_an_hour_old_codex_reading_shows_its_state_and_cap_until_the_max_age(monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_CODEX_READING_MAX_AGE_H", raising=False)
    quota = codex_quota.CodexQuota(1000, "pro", seven_day=QuotaWindow(used=10.0, resets_at=10**10))
    [row] = agents_quota.codex_rows([CodexAccount("default")], {"default": quota}, {}, now=1000 + 3600)
    assert (row.state, row.cap) == ("NORMAL", 6)
    [old] = agents_quota.codex_rows([CodexAccount("default")], {"default": quota}, {}, now=1000 + 6 * 3600 + 1)
    assert (old.state, old.cap) == ("STALE", None)


def test_claude_rows_carry_the_fable_window_and_a_stale_reading_drops_it():
    fable = QuotaWindow(60.0, 8000)
    result = ProbeResult("alpha", "allowed", "NORMAL", 70.0, QuotaWindow(20.0, 9000), QuotaWindow(30.0, 9000), fable)
    [fresh] = agents_quota.claude_rows([result], {}, "probe", {"alpha": 1000.0}, 1000.0, True)
    assert (fresh.fable_left, fresh.fable_resets_at) == (40.0, 8000)
    [stale] = agents_quota.claude_rows([result], {}, "probe", {"alpha": 1000.0}, 1000.0 + 3 * 3600, True)
    assert (stale.state, stale.fable_left, stale.fable_resets_at) == ("STALE", None, 8000)


def test_the_fable_view_shows_the_fable_window_under_a_dash_rule_without_trailing_spaces():
    row = agents_quota.QuotaRow(
        "claude", "alpha", "NORMAL", 0, None, None, None, "probe", cap=6, fable_left=40.0, fable_resets_at=4600
    )
    table = agents_quota.render([row], now=1000, fable=True).splitlines()
    reset = agents_quota._duration(4600, 1000).split()
    assert table[2].split()[-len(reset) - 1 :] == ["40%", *reset]
    assert set(table[1]) == {"-", " "}
    assert not any(line.endswith(" ") for line in table)


def test_a_zero_routing_left_sorts_after_a_small_positive_one():
    empty = agents_quota.QuotaRow("codex", "empty", "NORMAL", 0, None, None, None, "log", routing_left=0.0)
    some = agents_quota.QuotaRow("codex", "some", "NORMAL", 0, None, None, None, "log", routing_left=0.5)
    assert [row.account for row in agents_quota._ordered([empty, some])] == ["some", "empty"]


def test_a_reset_due_now_shows_a_dash():
    assert agents_quota._reset(50.0, 1000, 1000) == "-"
    assert agents_quota._reset(50.0, 1060, 1000) == agents_quota._duration(1060, 1000)


def test_live_tables_count_only_unlisted_codex_sessions_and_none_in_the_fable_view(monkeypatch):
    from hooks.context import account_sessions

    monkeypatch.setattr(account_sessions, "codex_sessions_by_account", lambda: {"alpha": 2, "ghost": 1})
    row = agents_quota.QuotaRow("codex", "alpha", "NORMAL", 0, None, None, None, "log")
    assert agents_quota._render_live([row], {}, 1000).splitlines()[-1] == "+1 sessions on unlisted account names"
    assert "unlisted" not in agents_quota._render_live([row], {}, 1000, fable=True)


def test_current_in_the_fable_view_adds_the_fable_columns(monkeypatch):
    monkeypatch.setattr(agents_quota, "_masters", lambda harness="": {})
    fresh = ProbeResult("beta", "allowed", "NORMAL", 60.0, QuotaWindow(20.0, 10**10), QuotaWindow(40.0, 10**10))
    table = agents_quota.current_table([], [fresh], {"beta"}, {}, True, "beta").splitlines()
    assert table[0].split() == [*HEADER, "FABLE", "LEFT", "FABLE", "RESET"]


def test_balance_table_defaults_to_the_codex_rows_at_its_own_time_and_counts_master_sessions(monkeypatch):
    from hooks.context import account_sessions
    from scripts.routing.master_account import MasterAccount

    seen = []
    codex = agents_quota.QuotaRow("codex", "personal", "NORMAL", 0, 60.0, 70.0, None, "log", cap=6, routing_left=70.0)
    master = MasterAccount("claude", "home", "", INTERACTIVE)
    monkeypatch.setattr(agents_quota, "_codex", lambda now: seen.append(now) or [codex])
    monkeypatch.setattr(agents_quota, "_masters", lambda harness="": {"claude": master})
    monkeypatch.setattr(account_sessions, "codex_sessions_by_account", lambda: {})
    result = ProbeResult("alpha", "allowed", "NORMAL", 70.0, QuotaWindow(20.0, 30_000), QuotaWindow(30.0, 90_000))
    cells = [line.split() for line in agents_quota.balance_table([result], {"home": 2}, 20_000.0).splitlines()[2:]]
    assert seen == [20_000.0]
    assert any(row[2] == "alpha" and "NORMAL" in row for row in cells)
    assert any("home" in row and "2/-" in row for row in cells)
    assert any(row[1:3] == ["codex", "personal"] for row in cells)


def test_quota_json_is_indented_two_spaces(monkeypatch, capsys):
    row = agents_quota.QuotaRow("codex", "alpha", "NORMAL", 0, None, 80.0, None, "log")
    monkeypatch.setattr(agents_quota, "_claude", lambda refresh, timeout: [])
    monkeypatch.setattr(agents_quota, "_codex", lambda now: [row])
    assert agents_quota.main(["--json"]) == 0
    assert capsys.readouterr().out.startswith('[\n  {\n    "agent": "codex",')
