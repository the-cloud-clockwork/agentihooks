from scripts import agents_quota, quota_state
from scripts import claude_quota_balancer as balancer
from scripts.claude_quota_balancer import ProbeResult, QuotaWindow
from scripts.swarm import capacity, drain_watch, quota_view
from scripts.swarm.health.findings import Limits


def test_the_vocabulary_names_four_states_and_drains_at_ten_percent_routing_left():
    assert (quota_state.OPEN, quota_state.DRAIN, quota_state.CLOSED, quota_state.UNKNOWN) == (
        "OPEN",
        "DRAIN",
        "CLOSED",
        "UNKNOWN",
    )
    assert quota_state.DRAIN_LEFT == 10
    assert [quota_state.state(6, left) for left in (0.0, 10.0, 10.5, None)] == ["DRAIN", "DRAIN", "OPEN", "OPEN"]
    assert quota_state.state(0, 80.0) == "CLOSED"
    assert quota_state.state(None, 5.0) == "UNKNOWN"


def test_draining_takes_an_operator_limit_and_never_drains_an_unknown_reading():
    assert quota_state.draining(10.0) and not quota_state.draining(10.5)
    assert quota_state.draining(12.0, 12) and not quota_state.draining(12.5, 12)
    assert not quota_state.draining(None)
    assert not quota_state.draining(None, 100)


def test_routing_left_is_the_week_for_codex_and_the_lower_window_for_claude():
    assert quota_state.routing_left("codex", 5.0, 40.0) == 40.0
    assert quota_state.routing_left("codex", None, 40.0) == 40.0
    assert quota_state.routing_left("claude", 5.0, 40.0) == 5.0
    assert quota_state.routing_left("claude", 50.0, 40.0) == 40.0
    assert quota_state.routing_left("claude", None, 40.0) is None
    assert quota_state.routing_left("claude", 40.0, None) is None


def test_every_quota_band_becomes_the_shared_state_and_a_diagnostic_state_stays_as_it_is():
    bands = ("NORMAL", "REDUCE", "DRAIN_SOON", "DRAIN", "BLOCKED")
    assert quota_state.BANDS == frozenset(bands)
    assert [quota_state.shown(band, 6, 9.0) for band in bands] == ["DRAIN"] * 5
    assert [quota_state.shown(band, 6, 50.0) for band in bands] == ["OPEN"] * 5
    assert quota_state.shown("DRAIN", 0, 2.0) == "CLOSED"
    assert quota_state.shown("BLOCKED", 0, 0.0) == "CLOSED"
    diagnostic = ("STALE", "SIGNED_OUT", "EXPIRED", "ERROR", "MASTERS_ONLY", "OPEN")
    assert [quota_state.shown(name, 6, 9.0) for name in diagnostic] == list(diagnostic)


def test_balance_swarm_status_and_the_drain_watch_agree_on_one_draining_account(monkeypatch):
    now = 100
    result = ProbeResult("a", "allowed", "DRAIN", 9.0, QuotaWindow(used=5.0), QuotaWindow(used=91.0))
    monkeypatch.setattr(balancer, "cached_observations", lambda **kw: [(now, result)])
    monkeypatch.setattr(balancer, "collect_results", lambda *args, **kwargs: ([], "cached"))
    monkeypatch.setattr(capacity.account_sessions, "sessions_by_account", lambda **_: {})
    monkeypatch.setattr(capacity.account_sessions, "codex_sessions_by_account", lambda: {})
    monkeypatch.setattr(capacity.codex_router, "accounts", lambda env: [])
    (row,) = capacity.accounts({"AH_CC_TOKEN_a": "fake"}, now)
    [balance] = agents_quota.claude_rows([result], {}, "cached", {"a": now}, now)
    stored = capacity.record(row)
    assert balance.state == row.state == "DRAIN"
    assert "  drain  routing 9% left" in quota_view._account_line(stored, [stored], now * 1000)
    assert drain_watch.draining(stored, Limits(), now)


def test_an_account_with_room_reads_open_everywhere():
    now = 1_000.0
    result = ProbeResult("alpha", "allowed", "REDUCE", 30.0, QuotaWindow(5.0, 20_000), QuotaWindow(70.0, 90_000))
    [balance] = agents_quota.claude_rows([result], {}, "cached", {"alpha": now}, now)
    state = capacity._state(balance.cap, quota_state.routing_left("claude", 95.0, 30.0))
    assert balance.state == state == "OPEN"
    assert capacity._state(balance.cap, quota_state.routing_left("codex", 5.0, 30.0)) == "OPEN"
    assert capacity._state(balance.cap, quota_state.routing_left("codex", 50.0, 8.0)) == "DRAIN"
    assert capacity._state(0, quota_state.routing_left("claude", 95.0, 30.0)) == "CLOSED"


def test_balance_reads_a_window_whose_reset_passed_as_full_like_capacity():
    now = 30_000.0
    result = ProbeResult("a", "allowed", "DRAIN", 7.0, QuotaWindow(93.0, 29_000), QuotaWindow(40.0, 90_000))
    [balance] = agents_quota.claude_rows([result], {}, "cached", {"a": now - 60}, now)
    assert balance.state == capacity._state(balance.cap, quota_state.routing_left("claude", 100.0, 60.0)) == "OPEN"
    assert balance.routing_left == 60.0
    [held] = agents_quota.claude_rows([result], {}, "cached", {"a": 27_900.0}, 28_000.0)
    assert held.state == "DRAIN"
    week_passed = ProbeResult("b", "allowed", "DRAIN", 5.0, QuotaWindow(50.0, 90_000), QuotaWindow(95.0, 29_000))
    [weekly] = agents_quota.claude_rows([week_passed], {}, "cached", {"b": now - 60}, now)
    assert (weekly.state, weekly.routing_left) == ("OPEN", 50.0)
