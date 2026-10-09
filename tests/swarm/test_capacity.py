import json
from dataclasses import replace

import pytest

from scripts import claude_quota_balancer as balancer
from scripts import session_bands
from scripts.swarm import capacity, notice_text
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm.tick import SpawnError, tick
from tests.swarm.test_tick import FakeLedger, FakeRuntime

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture(autouse=True)
def isolated_accounts(monkeypatch):
    monkeypatch.setattr(capacity.account_sessions, "codex_sessions_by_account", lambda: {})


def _store():
    import fakeredis

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


def account(name="a", cap=3, sessions=0, left=90, harness="claude"):
    return capacity.Account(harness, name, capacity._state(cap), sessions, left, left, cap)


def test_routed_session_reads_quota_for_every_occupied_account(monkeypatch, tmp_path):
    env = {"AGENTIHOOKS_HOME": str(tmp_path), "AH_CC_TOKEN_a": "fake-a"}
    windows = balancer.QuotaWindow(used=10)
    observations = [
        (100, balancer.ProbeResult(name, "allowed", "NORMAL", 90, windows, windows)) for name in ("a", "b", "c")
    ]
    balancer._write_cache(
        tmp_path / "claude-router-cache.json",
        {
            "version": 1,
            "modes": {
                "normal": {
                    "accounts": {
                        f"AH_CC_TOKEN_{r.account}": {"observed_at": at, "result": balancer._result_data(r)}
                        for at, r in observations
                    }
                }
            },
        },
    )
    monkeypatch.setattr(capacity.account_sessions, "sessions_by_account", lambda: {"a": 1, "b": 2, "c": 1})
    monkeypatch.setattr(capacity.codex_router, "routing_pool", lambda env: [])
    seen = capacity.accounts(env, 100)
    assert sorted((r.name, r.sessions, r.week_left) for r in seen) == [("a", 1, 90), ("b", 2, 90), ("c", 1, 90)]


@pytest.mark.parametrize(
    ("five_used", "expected_cap"),
    [(10, 6), (50, 4), (80, 3), (93, 2), (97, 0)],
)
def test_band_caps_follow_the_claude_five_hour_window_left(monkeypatch, five_used, expected_cap):
    result = balancer.ProbeResult(
        "a", "allowed", "NORMAL", 100 - five_used, balancer.QuotaWindow(used=five_used), balancer.QuotaWindow(used=0)
    )
    monkeypatch.setattr(balancer, "cached_observations", lambda **kw: [(100, result)])
    monkeypatch.setattr(capacity.account_sessions, "sessions_by_account", lambda: {})
    monkeypatch.setattr(capacity.codex_router, "routing_pool", lambda env: [])
    (row,) = capacity.accounts({}, 100)
    assert (row.cap, row.five_left, row.week_left) == (expected_cap, 100 - five_used, 100)
    assert row.state == ("CLOSED" if expected_cap == 0 else "OPEN")


def test_a_reset_window_raises_the_cap_and_the_effective_caps_on_the_next_tick(monkeypatch):
    five, week = balancer.QuotaWindow(used=98, resets_at=200), balancer.QuotaWindow(used=97, resets_at=250)
    result = balancer.ProbeResult("a", "allowed", "NORMAL", 2, five, week)
    monkeypatch.setattr(balancer, "cached_observations", lambda **kw: [(100, result)])
    monkeypatch.setattr(capacity.account_sessions, "sessions_by_account", lambda: {"a": 1})
    monkeypatch.setattr(capacity.codex_router, "routing_pool", lambda env: [])
    config = SwarmConfig("sw", "/repo", max_eng=3, max_ci=1, max_plan=0)
    agents = [AgentRecord("engineer", "eng", "e", harness="claude")]
    (drained,) = capacity.accounts({}, 150)
    (freed,) = capacity.accounts({}, 300)
    assert (drained.state, drained.cap, drained.week_resets_at) == ("CLOSED", 0, 250)
    assert (freed.state, freed.cap, freed.five_left, freed.week_left) == ("OPEN", 6, 100, 100)
    before = capacity.calculate(config, [drained], agents)
    after = capacity.calculate(config, [freed], agents)
    assert (before["effective"], before["placeable"]["claude"]) == ({"eng": 1, "ci": 0, "plan": 0}, 0)
    assert (after["effective"], after["placeable"]["claude"]) == ({"eng": 3, "ci": 1, "plan": 0}, 5)


def test_placement_spends_the_soonest_week_reset_first_only_above_the_handoff_margin():
    soon = replace(account("soon"), week_resets_at=1000)
    late = replace(account("late"), week_resets_at=9000)
    edge = capacity.Account("claude", "edge", "OPEN", 0, 5, 90, 2, 10)
    assert [seat.spend_before for seat in capacity.offered([soon, late, edge])] == [1000, 9000, None]
    config = SwarmConfig("sw", "/repo", max_eng=2, max_ci=0, max_plan=0)
    result = capacity.calculate(config, [late, edge, soon], [])
    assert [slot["account"] for slot in result["placements"]["eng"]] == ["soon", "late"]


def test_a_stale_claude_reading_gets_no_seat(monkeypatch):
    result = balancer.ProbeResult(
        "a", "allowed", "NORMAL", 90, balancer.QuotaWindow(used=10), balancer.QuotaWindow(used=0)
    )
    monkeypatch.setattr(balancer, "cached_observations", lambda **kw: [(100, result)])
    monkeypatch.setattr(capacity.account_sessions, "sessions_by_account", lambda: {})
    monkeypatch.setattr(capacity.codex_router, "routing_pool", lambda env: [])
    from scripts import session_bands

    stale_at = 100 + session_bands.FRESH_SECONDS + 1
    seen = capacity.accounts({}, stale_at)
    assert seen == [capacity.Account("claude", "a", "UNKNOWN", 0, 90, 100, None)]


def test_a_fresh_codex_reading_with_only_the_week_gets_the_top_band(monkeypatch):
    from scripts.codex_quota import CodexQuota

    monkeypatch.setattr(balancer, "cached_observations", lambda **kw: [])
    monkeypatch.setattr(capacity.account_sessions, "sessions_by_account", lambda: {})
    pool = [capacity.codex_router.CodexAccount("a", "AH_CX_TOKEN_a")]
    monkeypatch.setattr(capacity.codex_router, "routing_pool", lambda env: pool)
    weekly = CodexQuota(100, "pro", seven_day=balancer.QuotaWindow(used=10, resets_at=500))
    monkeypatch.setattr(capacity.codex_router, "quotas", lambda accounts, env: {"a": weekly})
    monkeypatch.setattr(capacity.codex_router, "probe", lambda *a, **kw: pytest.fail("reached the real codex probe"))
    seen = capacity.accounts({}, 100)
    assert seen == [capacity.Account("codex", "a", "OPEN", 0, None, 90, 6, 500)]


def test_accounts_judge_every_window_at_the_given_time_and_pass_the_environment(monkeypatch):
    from scripts.codex_quota import CodexQuota

    now, env, calls = 1_000_000, {"AH": "1"}, []
    claude = balancer.ProbeResult(
        "a", "allowed", "NORMAL", 5.0, balancer.QuotaWindow(95.0, now + 100), balancer.QuotaWindow(90.0, now - 10)
    )
    codex = CodexQuota(now, "pro", balancer.QuotaWindow(40.0, now + 100), balancer.QuotaWindow(30.0, now - 10))
    pool = [capacity.codex_router.CodexAccount("default")]
    monkeypatch.setattr(balancer, "discover_credentials", lambda environ: [])
    monkeypatch.setattr(balancer, "cached_observations", lambda **kw: [(now, claude)])
    monkeypatch.setattr(capacity.account_sessions, "sessions_by_account", lambda: {})
    monkeypatch.setattr(capacity.account_sessions, "codex_sessions_by_account", lambda: {"x": 1})
    monkeypatch.setattr(capacity.codex_router, "routing_pool", lambda environ: calls.append(("pool", environ)) or pool)
    monkeypatch.setattr(
        capacity.codex_router,
        "fresh_quotas",
        lambda p, environ, at: calls.append(("fresh", environ, at)) or {"default": codex},
    )
    monkeypatch.setattr(
        capacity.codex_router, "quotas", lambda p, environ: calls.append(("quotas", [a.name for a in p], environ)) or {}
    )
    assert capacity.accounts(env, now) == [
        capacity.Account("claude", "a", "OPEN", 0, 5.0, 100.0, 2),
        capacity.Account("codex", "default", "OPEN", 0, 60.0, 100.0, 6),
        capacity.Account("codex", "x", "UNKNOWN", 1, None, None, None),
    ]
    assert calls == [("pool", env), ("fresh", env, now), ("quotas", ["x"], env)]
    calls.clear()
    capacity.accounts(env, now, refresh=False)
    assert calls[1] == ("quotas", ["default"], env)


def test_a_stale_codex_reading_gets_no_seat(monkeypatch):
    from scripts import session_bands
    from scripts.codex_quota import CodexQuota

    monkeypatch.setattr(balancer, "cached_observations", lambda **kw: [])
    monkeypatch.setattr(capacity.account_sessions, "sessions_by_account", lambda: {})
    pool = [capacity.codex_router.CodexAccount("a", "AH_CX_TOKEN_a")]
    monkeypatch.setattr(capacity.codex_router, "routing_pool", lambda env: pool)
    stale_at = 100 + session_bands.FRESH_SECONDS + 1
    stale = CodexQuota(100, "pro", seven_day=balancer.QuotaWindow(used=10))
    monkeypatch.setattr(capacity.codex_router, "quotas", lambda accounts, env: {"a": stale})
    monkeypatch.setattr(capacity.codex_router, "probe", lambda *a, **kw: None)
    seen = capacity.accounts({}, stale_at)
    assert seen == [capacity.Account("codex", "a", "UNKNOWN", 0, None, 90, None)]


def test_quota_capacity_with_all_zero_demand_never_refreshes_codex(tmp_path, monkeypatch):
    from scripts.swarm import runtime as module

    calls = []
    monkeypatch.setattr(balancer, "discover_credentials", lambda environ: [])
    monkeypatch.setattr(balancer, "collect_results", lambda *a, **kw: ([], "none"))
    monkeypatch.setattr(balancer, "cached_observations", lambda **kw: [])
    monkeypatch.setattr(capacity.account_sessions, "sessions_by_account", lambda: {})
    monkeypatch.setattr(capacity.codex_router, "routing_pool", lambda env: [])
    monkeypatch.setattr(capacity.codex_router, "fresh_quotas", lambda *a, **kw: calls.append(1) or {})
    monkeypatch.setattr(capacity.codex_router, "probe", lambda *a, **kw: pytest.fail("reached the real codex probe"))
    rt = module.HerdrRuntime(home=tmp_path)
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0)
    rt.quota_capacity(config, [], 100, {"eng": 0, "ci": 0, "plan": 0})
    assert calls == []


def test_quota_capacity_with_nonzero_demand_refreshes_codex(tmp_path, monkeypatch):
    from scripts.swarm import runtime as module

    calls = []
    monkeypatch.setattr(balancer, "discover_credentials", lambda environ: [])
    monkeypatch.setattr(balancer, "collect_results", lambda *a, **kw: ([], "none"))
    monkeypatch.setattr(balancer, "cached_observations", lambda **kw: [])
    monkeypatch.setattr(capacity.account_sessions, "sessions_by_account", lambda: {})
    monkeypatch.setattr(capacity.codex_router, "routing_pool", lambda env: [])
    monkeypatch.setattr(capacity.codex_router, "fresh_quotas", lambda *a, **kw: calls.append(1) or {})
    rt = module.HerdrRuntime(home=tmp_path)
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0)
    rt.quota_capacity(config, [], 100, {"eng": 1, "ci": 0, "plan": 0})
    assert calls == [1]


def test_one_seat_goes_to_the_empty_lane_before_another_engineer():
    config = SwarmConfig("sw", "/repo", max_eng=2, max_ci=1, max_plan=1)
    agents = [AgentRecord("engineer", "eng", "e", harness="claude")]
    result = capacity.calculate(config, [account(sessions=2)], agents)
    assert result["effective"] == {"eng": 1, "ci": 1, "plan": 0}


def test_reduced_caps_do_not_retire_running_agents_and_comment_once(monkeypatch):
    store = _store()
    config = SwarmConfig("sw", "/repo", max_eng=2, max_ci=1, max_plan=0, state="running")
    store.create(config)
    ledger = FakeLedger([{"id": "e"}, {"id": "e2"}, {"id": "c", "lane": "ci"}])
    ledger.comments = []
    ledger.comment = lambda slug, item, text, by: ledger.comments.append((slug, item, text, by))
    runtime = FakeRuntime()
    monkeypatch.setattr(capacity, "accounts", lambda env, now: [account(sessions=1)])
    runtime.quota_capacity = lambda cfg, agents, now, demand, requirements: capacity.calculate(
        cfg, capacity.accounts({}, now), agents, demand
    )
    tick("sw", store, ledger, runtime, 1000)
    assert len(runtime.spawned) == 2
    monkeypatch.setattr(capacity, "accounts", lambda env, now: [account(cap=0)])
    tick("sw", store, ledger, runtime, 2000)
    assert not runtime.killed
    assert len(ledger.comments) == 2
    tick("sw", store, ledger, runtime, 3000)
    assert len(ledger.comments) == 2
    assert store.config("sw").max_eng == 2
    monkeypatch.setattr(capacity, "accounts", lambda env, now: [account()])
    tick("sw", store, ledger, runtime, 4000)
    assert len(ledger.comments) == 3


def test_automatic_harness_falls_through_to_a_placeable_account(tmp_path, monkeypatch):
    from scripts.swarm import runtime as module

    rt = module.HerdrRuntime(home=tmp_path, choose=lambda *_: ("claude", "priority"))
    rt._quota_accounts = [account(cap=0), account("cx", harness="codex")]
    seen = []
    monkeypatch.setattr(
        rt,
        "_launch",
        lambda config, lane, task, name, argv, **kw: seen.append(argv) or module.Placed("pane", "codex", "cx"),
    )
    monkeypatch.setattr(module.plugins, "claude_only", lambda _: False)
    config = SwarmConfig("sw", str(tmp_path), max_eng=2, max_ci=0, max_plan=0, code="a1b2c3")
    placed = rt.spawn(config, "plan", "planner@a1b2c3-0001", {"id": "p", "title": "Plan"})
    assert placed.harness == "codex"
    assert seen[0][seen[0].index("--route") + 1] == "cx"
    assert rt._quota_accounts[1].sessions == 1


def test_effective_caps_are_exposed_in_status(monkeypatch):
    from scripts.swarm import status

    store = _store()
    store.create(SwarmConfig("sw", "/repo", max_eng=2, max_ci=1))
    decision = {"effective": {"eng": 0, "ci": 0, "plan": 0}, "reason": "accounts are closed", "accounts": []}
    store.redis.set(store.key("sw", "quota-capacity"), json.dumps(decision))
    monkeypatch.setattr(status, "page_quota", lambda: {})
    assert status.status_report(store, "sw", {"tasks": []})["quota_capacity"] == {
        **decision,
        "lanes": ["eng", "ci", "plan"],
    }
    assert capacity.status_line(decision) == "quota capacity eng 0 ci 0 plan 0 because accounts are closed"


def test_each_tick_refreshes_the_balance_source_for_all_available_accounts(monkeypatch):
    seen = []
    environ = {"AH_CC_TOKEN_a": "fake-a", "AH_CC_TOKEN_b": "fake-b"}
    monkeypatch.setattr(
        balancer,
        "collect_results",
        lambda credentials, **kwargs: (seen.append(([r.account for r in credentials], kwargs)) or [], "live"),
    )
    monkeypatch.setattr(balancer, "cached_observations", lambda **kwargs: [])
    monkeypatch.setattr(capacity.account_sessions, "sessions_by_account", lambda: {})
    monkeypatch.setattr(capacity.codex_router, "routing_pool", lambda _: [])
    capacity.accounts(environ, 123)
    assert seen == [(["a", "b"], {"environ": environ, "now": 123})]


def test_idle_lanes_never_reserve_the_only_seat():
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=1, max_plan=0)
    result = capacity.calculate(config, [account(sessions=2)], [], demand={"eng": 0, "ci": 1, "plan": 0})
    assert result["effective"] == {"eng": 0, "ci": 1, "plan": 0}


def test_automatic_lanes_preserve_seats_required_by_fixed_lanes():
    config = SwarmConfig("sw", "/repo", max_eng=2, max_ci=2, max_plan=0, lanes={"ci": {"agent": "claude"}})
    result = capacity.calculate(config, [account(sessions=1), account("cx", sessions=1, harness="codex")], [])
    assert result["effective"] == {"eng": 2, "ci": 2, "plan": 0}
    assert result["allocation"] == {
        "eng": {"claude": 0, "codex": 2},
        "ci": {"claude": 2, "codex": 0},
        "plan": {"claude": 0, "codex": 0},
    }


def test_placements_rotate_the_account_with_fewest_sessions_within_one_tick():
    config = SwarmConfig("sw", "/repo", max_eng=2, max_ci=0, max_plan=0)
    decision = capacity.calculate(config, [account("a"), account("b")], [])
    assert decision["placements"]["eng"] == [
        {"index": 0, "harness": "claude", "account": "a"},
        {"index": 1, "harness": "claude", "account": "b"},
    ]


def test_a_failed_capacity_comment_never_loses_the_saved_decision():
    store = _store()
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0)
    store.create(config)
    ledger = FakeLedger([{"id": "e"}])
    runtime = FakeRuntime()
    runtime.quota_capacity = lambda cfg, agents, now, demand, requirements: capacity.calculate(
        cfg, [account()], agents, demand
    )
    ledger.comment = lambda *args, **kw: (_ for _ in ()).throw(RuntimeError("ledger unavailable"))
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        capacity.apply("sw", config, store, ledger, runtime, 1000)
    assert capacity.read(store, "sw")["at"] == 1000
    comments = []
    ledger.comment = lambda *args, **kw: comments.append((args, kw))
    assert capacity.apply("sw", config, store, ledger, runtime, 2000) == []
    assert comments == []


def test_refused_capacity_comment_keeps_the_fresh_decision(capsys):
    from scripts.swarm.ledger_client import LedgerRefused

    store = _store()
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0)
    store.create(config)
    ledger = FakeLedger([{"id": "e"}])
    runtime = FakeRuntime()
    runtime.quota_capacity = lambda cfg, agents, now, demand, requirements: capacity.calculate(
        cfg, [account()], agents, demand
    )

    def refuse(slug, task, text, now_ms):
        assert (slug, task, now_ms) == ("sw", "e", 1000)
        raise LedgerRefused("plain words refused")

    ledger.capacity_comment = refuse
    assert len(capacity.apply("sw", config, store, ledger, runtime, 1000)) == 1
    assert capsys.readouterr().err == "swarm notice dropped, the ledger refused it: plain words refused\n"
    assert capacity.read(store, "sw")["tasks"] == {"e": "claude"}
    assert capacity.read(store, "sw")["at"] == 1000


def test_codex_accounts_with_live_sessions_keep_their_own_quotas(monkeypatch):
    from scripts.codex_quota import CodexQuota

    monkeypatch.setattr(balancer, "cached_observations", lambda **kw: [])
    monkeypatch.setattr(capacity.account_sessions, "sessions_by_account", lambda: {})
    monkeypatch.setattr(capacity.account_sessions, "codex_sessions_by_account", lambda: {"a": 1, "b": 2})
    monkeypatch.setattr(
        capacity.codex_router, "routing_pool", lambda _: [capacity.codex_router.CodexAccount("a", "AH_CX_TOKEN_a")]
    )

    def quotas(pool, environ):
        assert {row.name for row in pool} <= {"a", "b"}
        return {
            row.name: CodexQuota(100, "pro", balancer.QuotaWindow(used=10), balancer.QuotaWindow(used=20))
            for row in pool
        }

    monkeypatch.setattr(capacity.codex_router, "quotas", quotas)
    monkeypatch.setattr(capacity.codex_router, "probe", lambda *a, **kw: pytest.fail("reached the real codex probe"))
    seen = capacity.accounts({}, 100)
    assert [(row.name, row.sessions, row.week_left, row.cap) for row in seen] == [("a", 1, 80, 6), ("b", 2, 80, None)]
    assert [(seat.account, seat.free) for seat in capacity.offered(seen)] == [("a", 5), ("b", 0)]


def test_runtime_honors_reserved_harness_seats(tmp_path):
    from scripts.swarm.runtime import HerdrRuntime

    runtime = HerdrRuntime(home=tmp_path)
    runtime._quota_accounts = [account(), account("cx", harness="codex")]
    runtime._quota_allocations = {"eng": {"claude": 0, "codex": 1}, "ci": {"claude": 1, "codex": 0}}
    assert runtime._quota_choice("claude", "priority", False, "eng") == (
        "codex",
        "fallthrough: claude has no placeable quota seats",
    )
    assert runtime._quota_choice("claude", "requested", True, "ci") == ("claude", "requested")


def test_runtime_refuses_an_account_when_its_harness_has_no_free_seat(tmp_path):
    from scripts.swarm.runtime import HerdrRuntime

    runtime = HerdrRuntime(home=tmp_path)
    runtime._quota_accounts = [account(sessions=3), account("cx", harness="codex")]
    with pytest.raises(SpawnError, match="no claude account has placeable quota seats") as error:
        runtime._quota_account("claude", None, None)
    assert error.value.status == "unavailable"
    assert runtime._quota_account("codex", None, None).name == "cx"


def test_failed_fresh_probe_does_not_leave_a_stale_healthy_account_placeable(monkeypatch):
    healthy = balancer.ProbeResult(
        "a", "allowed", "NORMAL", 90, balancer.QuotaWindow(used=10), balancer.QuotaWindow(used=10)
    )
    failed = balancer.ProbeResult("a", "rejected", "BLOCKED", 0, balancer.QuotaWindow(), balancer.QuotaWindow())
    monkeypatch.setattr(balancer, "collect_results", lambda *args, **kw: ([failed], "live"))
    monkeypatch.setattr(balancer, "cached_observations", lambda **kw: [(100, healthy)])
    monkeypatch.setattr(capacity.account_sessions, "sessions_by_account", lambda: {})
    monkeypatch.setattr(capacity.account_sessions, "codex_sessions_by_account", lambda: {})
    monkeypatch.setattr(capacity.codex_router, "routing_pool", lambda _: [])
    seen = capacity.accounts({"AH_CC_TOKEN_a": "fake-a"}, 200)
    assert seen[0].state == "CLOSED"
    assert capacity.free_seats(seen[0]) == 0


@pytest.mark.parametrize("saved", [False, True])
def test_profile_and_saved_handoff_harnesses_are_reserved_before_automatic_work(tmp_path, monkeypatch, saved):
    from scripts.swarm.runtime import HerdrRuntime

    runtime = HerdrRuntime(home=tmp_path)
    monkeypatch.setattr("scripts.swarm.runtime.plugins.claude_only", lambda profile: profile == "frontend")
    task = {"id": "e", "profile": "frontend"}
    if saved:
        task = {"id": "e", "handoff_envelope": {"launch": {"profile": "engineer", "harness": "claude"}}}
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=1, max_plan=0)
    requirements = runtime.quota_requirements(config, {"eng": [task], "ci": [{"id": "c"}], "plan": []})
    assert requirements == {"eng": [("claude",)], "ci": [("claude", "codex")], "plan": []}
    observed = [account(sessions=2), account("cx", sessions=2, harness="codex")]
    decision = capacity.calculate(config, observed, [], {"eng": 1, "ci": 1, "plan": 0}, requirements)
    runtime._quota_accounts = observed
    runtime._quota_allocations = decision["allocation"]
    assert runtime._quota_choice("claude", "required", True, "eng") == ("claude", "required")
    assert runtime._quota_choice("claude", "priority", False, "ci")[0] == "codex"
    assert decision["effective"] == {"eng": 1, "ci": 1, "plan": 0}


def test_unobserved_live_account_stays_visible_and_unplaceable(monkeypatch):
    monkeypatch.setattr(balancer, "cached_observations", lambda **kw: [])
    monkeypatch.setattr(capacity.account_sessions, "sessions_by_account", lambda: {"unknown": 2})
    monkeypatch.setattr(capacity.codex_router, "routing_pool", lambda env: [])
    assert capacity.accounts({}, 100) == [capacity.Account("claude", "unknown", "UNKNOWN", 2, None, None)]


def test_closed_and_reduced_free_seats_follow_the_accounts_own_cap():
    closed = capacity.Account("claude", "a", "CLOSED", 0, 90, 90, cap=0)
    open_ = capacity.Account("codex", "cx", "OPEN", 1, 30, 30, cap=5)
    assert capacity.free_seats(closed) == 0
    assert capacity.free_seats(open_) == 4


def test_finished_agents_do_not_reserve_capacity_and_reason_lists_all_restrictions():
    config = SwarmConfig("sw", "/repo", max_eng=2, max_ci=1, max_plan=0)
    dead = [AgentRecord("finished", "eng", "e", state="finished")]
    seen = [account("z", cap=0), account("a"), account("b")]
    decision = capacity.calculate(config, seen, dead)
    assert decision["configured"] == {"eng": 2, "ci": 1, "plan": 0}
    assert decision["effective"] == {"eng": 2, "ci": 1, "plan": 0}
    assert decision["placeable"] == {"claude": 6, "codex": 0}
    assert decision["reason"] == "accounts are closed; Claude has 6 free seats and Codex has 0 free seats"
    assert decision["accounts"] == [
        {
            "harness": "claude",
            "name": name,
            "state": state,
            "sessions": 0,
            "five_left": 90,
            "week_left": 90,
            "cap": cap,
            "week_resets_at": None,
        }
        for name, state, cap in (("z", "CLOSED", 0), ("a", "OPEN", 3), ("b", "OPEN", 3))
    ]
    placed_accounts = {name for lane in decision["placements"].values() for slot in lane for name in [slot["account"]]}
    assert placed_accounts == {"a", "b"}
    assert sum(decision["allocation"]["eng"].values()) == 2
    assert sum(decision["allocation"]["ci"].values()) == 1


def test_runtime_capacity_uses_the_current_environment_and_saves_the_allocation(tmp_path, monkeypatch):
    from scripts.swarm import runtime as module

    seen = [account(), account("cx", harness="codex")]

    def observations(env, now, refresh=True):
        assert now == 123
        return seen

    monkeypatch.setattr(capacity, "accounts", observations)
    rt = module.HerdrRuntime(home=tmp_path, choose=lambda *args: ("claude", module.agent_choice.ALL_FULL))
    config = SwarmConfig("sw", "/repo", max_eng=2, max_ci=1, max_plan=0)
    decision = rt.quota_capacity(
        config, [], 123, {"eng": 1, "ci": 0, "plan": 0}, {"eng": [("claude",)], "ci": [], "plan": []}
    )
    assert decision["effective"] == {"eng": 1, "ci": 0, "plan": 0}
    assert rt._quota_accounts == seen
    assert rt._quota_allocations == {
        "eng": {"claude": 1, "codex": 0},
        "ci": {"claude": 0, "codex": 0},
        "plan": {"claude": 0, "codex": 0},
    }
    assert rt.has_capacity(config)
    rt._quota_accounts = [account(cap=0)]
    assert not rt.has_capacity(config)


def test_status_command_prints_the_capacity_reason(monkeypatch, capsys):
    from types import SimpleNamespace

    from scripts.swarm import cli

    store = _store()
    store.create(SwarmConfig("sw", "/repo", max_eng=2, max_ci=1))
    ledger = FakeLedger([])
    monkeypatch.setattr(cli, "LedgerClient", lambda: ledger)
    store.redis.set(
        store.key("sw", "quota-capacity"),
        '{"configured":{"eng":2,"ci":1,"plan":1},"effective":{"eng":0,"ci":0,"plan":0},'
        '"reason":"accounts are closed","accounts":[],"at":0}',
    )
    monkeypatch.setattr(cli, "now_ms", lambda: 3 * 60_000)
    cli.cmd_status(store, SimpleNamespace(slug="sw", json=False))
    assert (
        "quota capacity eng 0 of 2, ci 0 of 1, plan 0 of 1, changed 3 minutes ago, because accounts are closed"
        in capsys.readouterr().out.splitlines()
    )


def test_unplaceable_first_task_does_not_block_other_ready_work(monkeypatch, tmp_path):
    from scripts.swarm.runtime import HerdrRuntime
    from scripts.swarm.tick import _spawn_order

    config = SwarmConfig("sw", "/repo", max_eng=2, max_ci=0, max_plan=0)
    runtime = HerdrRuntime(home=tmp_path)
    monkeypatch.setattr("scripts.swarm.runtime.plugins.claude_only", lambda profile: profile == "frontend")
    ready = {
        "eng": [{"id": "fixed", "profile": "frontend"}, {"id": "auto", "profile": "engineer"}],
        "ci": [],
        "plan": [],
    }
    requirements = runtime.quota_requirements(config, ready)
    monkeypatch.setattr(
        capacity, "accounts", lambda env, now, refresh=True: [account(cap=0), account("cx", harness="codex")]
    )
    decision = runtime.quota_capacity(config, [], 100, {"eng": 2, "ci": 0, "plan": 0}, requirements)
    assert decision["effective"] == {"eng": 1, "ci": 0, "plan": 0}
    assert decision["placements"] == {"eng": [{"index": 1, "harness": "codex", "account": "cx"}], "ci": [], "plan": []}
    assert decision["tasks"] == {"auto": "codex"}
    store = _store()
    store.create(config)
    ledger = FakeLedger(ready["eng"])
    from json import dumps

    store.redis.set(store.key("sw", "quota-capacity"), dumps(decision))
    rows = ledger.rows
    assert [(lane, task["id"]) for lane, task in _spawn_order("sw", config, store, [], rows, ledger.state("sw"))] == [
        ("eng", "auto")
    ]


def test_runtime_preserves_flexible_first_then_fixed_task_reservations(monkeypatch, tmp_path):
    from scripts.swarm import runtime as module

    runtime = module.HerdrRuntime(home=tmp_path, choose=lambda *_: ("claude", "priority"))
    monkeypatch.setattr(module.plugins, "claude_only", lambda profile: profile == "frontend")
    monkeypatch.setattr(
        module.profile_choice,
        "choose",
        lambda slug, lane, chosen, task, env, overlays=None: module.profile_choice.ProfileDecision(
            task["profile"], "task", "explicit"
        ),
    )
    config = SwarmConfig("sw", str(tmp_path), max_eng=2, max_ci=0, max_plan=0, code="a1b2c3")
    ready = {
        "eng": [
            {"id": "auto", "title": "Auto", "profile": "engineer"},
            {"id": "fixed", "title": "Fixed", "profile": "frontend"},
        ],
        "ci": [],
        "plan": [],
    }
    requirements = runtime.quota_requirements(config, ready)
    monkeypatch.setattr(
        capacity,
        "accounts",
        lambda env, now, refresh=True: [account(sessions=2), account("cx", sessions=2, harness="codex")],
    )
    runtime.quota_capacity(config, [], 100, {"eng": 2, "ci": 0, "plan": 0}, requirements)
    seen = []

    def launch(cfg, lane, task, name, argv, **kwargs):
        harness = argv[argv.index("--agent") + 1]
        seen.append((task, harness, argv[argv.index("--route") + 1]))
        return module.Placed("pane", harness, seen[-1][2])

    monkeypatch.setattr(runtime, "_launch", launch)
    choices = []
    for index, task in enumerate(ready["eng"], 1):
        choices.append(runtime.spawn(config, "eng", f"engineer@a1b2c3-{index:04}", task).choice)
    assert choices[0] == "overflow"
    assert seen == [("auto", "codex", "cx"), ("fixed", "claude", "a")]
    assert runtime._quota_allocations["eng"] == {"claude": 0, "codex": 0}


def _runtime_probe(tmp_path, monkeypatch, observations, reason="priority"):
    from scripts.swarm import runtime as module

    runtime = module.HerdrRuntime(home=tmp_path, choose=lambda requested, env: (requested or "claude", reason))
    runtime._quota_accounts = observations
    monkeypatch.setattr(module.plugins, "claude_only", lambda profile: profile == "frontend")
    monkeypatch.setattr(
        module.profile_choice,
        "choose",
        lambda slug, lane, chosen, task, env, overlays=None: module.profile_choice.ProfileDecision(
            task.get("profile", "planner"), "task", "explicit"
        ),
    )
    seen = []

    def launch(cfg, lane, task, name, argv, **kwargs):
        harness = argv[argv.index("--agent") + 1]
        route = argv[argv.index("--route") + 1]
        seen.append((task, harness, route))
        return module.Placed("pane", harness, route)

    monkeypatch.setattr(runtime, "_launch", launch)
    config = SwarmConfig("sw", str(tmp_path), max_eng=0, max_ci=0, max_plan=2, code="a1b2c3")
    return runtime, config, seen


def test_saved_healthy_account_wins_over_another_accounts_higher_quota(tmp_path, monkeypatch):
    runtime, config, seen = _runtime_probe(tmp_path, monkeypatch, [account("a", left=40), account("b")])
    saved = {"profile": "planner", "harness": "claude", "model": "fable", "effort": "high", "account": "a"}
    task = {"id": "p", "title": "Continue", "handoff_envelope": {"launch": saved}}
    runtime.spawn(config, "plan", "planner@a1b2c3-0001", task)
    assert seen == [("p", "claude", "a")]
    assert runtime._quota_accounts == [account("a", sessions=1, left=40), account("b")]


def test_spawn_routes_to_the_planned_account_then_rotates_the_next_automatic_pick(tmp_path, monkeypatch):
    runtime, config, seen = _runtime_probe(tmp_path, monkeypatch, [account("a"), account("b")], reason="priority")
    runtime._quota_tasks = {"p1": "claude"}
    runtime._quota_task_accounts = {"p1": "b"}
    runtime.spawn(config, "plan", "planner@a1b2c3-0001", {"id": "p1", "title": "Plan"})
    runtime.spawn(config, "plan", "planner@a1b2c3-0002", {"id": "p2", "title": "Plan"})
    assert seen == [("p1", "claude", "b"), ("p2", "claude", "a")]


def test_failed_launch_does_not_consume_account_or_reserved_slots(tmp_path, monkeypatch):
    from scripts.swarm.tick import SpawnError

    observed = [account()]
    runtime, config, seen = _runtime_probe(tmp_path, monkeypatch, observed)
    runtime._quota_allocations = {"plan": {"claude": 1, "codex": 0}}

    def fail(*args, **kwargs):
        raise SpawnError("launch failed")

    monkeypatch.setattr(runtime, "_launch", fail)
    with pytest.raises(SpawnError, match="^launch failed$"):
        runtime.spawn(config, "plan", "planner@a1b2c3-0001", {"id": "p", "title": "Plan"})
    assert runtime._quota_accounts == observed
    assert runtime._quota_allocations == {"plan": {"claude": 1, "codex": 0}}


@pytest.mark.parametrize("fixed", ["profile", "lane", "saved"])
def test_a_fixed_claude_task_never_falls_through_to_codex(tmp_path, monkeypatch, fixed):
    from scripts.swarm.tick import SpawnError

    runtime, config, seen = _runtime_probe(tmp_path, monkeypatch, [account(cap=0), account("cx", harness="codex")])
    task = {"id": "p", "title": "Plan"}
    if fixed == "profile":
        task["profile"] = "frontend"
    elif fixed == "lane":
        config = replace(config, lanes={"plan": {"agent": "claude"}})
    else:
        task["handoff_envelope"] = {
            "launch": {"profile": "planner", "harness": "claude", "model": "fable", "effort": "high"}
        }
    with pytest.raises(SpawnError, match="^no claude account has placeable quota seats$") as error:
        runtime.spawn(config, "plan", "planner@a1b2c3-0001", task)
    assert error.value.status == "unavailable"
    assert seen == []


def test_verified_account_seats_override_an_old_harness_cap_choice(tmp_path, monkeypatch):
    from scripts import agent_choice

    runtime, config, seen = _runtime_probe(tmp_path, monkeypatch, [account()], reason=agent_choice.ALL_FULL)
    runtime.spawn(config, "plan", "planner@a1b2c3-0001", {"id": "p", "title": "Plan"})
    assert seen == [("p", "claude", "a")]


def test_capacity_evidence_has_the_swarm_task_actor_and_stable_time():
    store = _store()
    config = SwarmConfig("sw", "/repo", max_eng=2, max_ci=1, max_plan=0)
    store.create(config)
    ledger = FakeLedger([{"id": "old", "done": True, "state": "done"}, {"id": "e"}])
    comments = []

    def state(slug):
        assert slug == "sw"
        return {"tasks": list(ledger.rows.values())}

    ledger.state = state
    ledger.comment = lambda *args, **kwargs: comments.append((args, kwargs))
    runtime = FakeRuntime()

    def quota(cfg, agents, now, demand, requirements):
        assert cfg == config and agents == [] and now in (1, 2)
        assert demand == {"eng": 1, "ci": 0, "plan": 0} and requirements is None
        return capacity.calculate(cfg, [account()], agents, demand)

    runtime.quota_capacity = quota
    expected = "quota capacity eng 1 ci 0 plan 0 because accounts have quota; Claude has 3 free seats and Codex has 0 free seats"
    assert capacity.apply("sw", config, store, ledger, runtime, 1000) == [expected]
    assert comments == [(("sw", "e", expected), {"by": "swarm"})]
    assert capacity.read(store, "sw")["at"] == 1000
    assert capacity.read(store, "sw")["tasks"] == {"e": "claude"}
    assert capacity.apply("sw", config, store, ledger, runtime, 2000) == []
    assert capacity.read(store, "sw")["at"] == 1000
    assert len(comments) == 1
    assert capacity.status_line({}) == "quota capacity has not been observed"


def test_capacity_can_comment_after_every_task_has_closed():
    store = _store()
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0)
    store.create(config)
    ledger = FakeLedger([{"id": "done", "done": True, "state": "done"}])
    comments = []
    ledger.comment = lambda *args, **kwargs: comments.append((args, kwargs))
    runtime = FakeRuntime()
    runtime.quota_capacity = lambda cfg, agents, now, demand, requirements: capacity.calculate(cfg, [], agents, demand)
    text = "quota capacity eng 0 ci 0 plan 0 because accounts have quota; Claude has 0 free seats and Codex has 0 free seats"
    assert capacity.apply("sw", config, store, ledger, runtime, 1000) == [text]
    assert comments == [(("sw", "done", text), {"by": "swarm"})]


def test_capacity_comment_uses_controller_authority(monkeypatch):
    from types import SimpleNamespace

    from scripts.swarm import ledger_client

    calls = []

    def call(slug, ops, service):
        calls.append((slug, ops, service))
        return {}

    monkeypatch.setattr(ledger_client, "_ledger", lambda: SimpleNamespace(call=call))
    client = ledger_client.LedgerClient()
    client.capacity_comment("sw", "e", "quota capacity changed", 1000)
    assert len(calls) == 1
    slug, ops, service = calls[0]
    assert slug == "sw" and service is True
    assert ops[0]["thread"] == "tasks/e/comments"
    assert ops[0]["by"] == "swarm"
    assert ops[0]["text"] == "quota capacity changed"


def test_the_capacity_comment_passes_the_ledger_schema(monkeypatch):
    from types import SimpleNamespace

    from scripts.swarm import ledger_client

    ledger_client._ledger()
    import ledger_core

    sent = []
    monkeypatch.setattr(
        ledger_client, "_ledger", lambda: SimpleNamespace(call=lambda slug, ops, service: sent.extend(ops) or {})
    )
    store = _store()
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0)
    store.create(config)
    runtime = FakeRuntime()
    runtime.quota_capacity = lambda cfg, agents, now, demand, requirements: capacity.calculate(
        cfg, [account()], agents, demand
    )
    ledger = FakeLedger([{"id": "e"}])
    ledger.capacity_comment = ledger_client.LedgerClient().capacity_comment
    capacity.apply("sw", config, store, ledger, runtime, 1000)
    (op,) = sent
    ledger_core.check_op(op)


def test_warned_capacity_comment_passes_the_ledger_schema(monkeypatch):
    from types import SimpleNamespace

    from scripts.swarm import ledger_client

    ledger_client._ledger()
    import ledger_core

    sent = []
    monkeypatch.setattr(
        ledger_client, "_ledger", lambda: SimpleNamespace(call=lambda slug, ops, service: sent.extend(ops) or {})
    )
    store = _store()
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0)
    store.create(config)
    runtime = FakeRuntime()
    runtime.quota_capacity = lambda cfg, agents, now, demand, requirements: capacity.calculate(
        cfg,
        [account("a"), account("b", harness="codex")],
        agents,
        demand,
        warned={("claude", "a"): "weekly", ("codex", "b"): "weekly"},
    )
    ledger = FakeLedger([{"id": "e"}])
    ledger.capacity_comment = ledger_client.LedgerClient().capacity_comment
    capacity.apply("sw", config, store, ledger, runtime, 1000)
    (op,) = sent
    ledger_core.check_op(op)


def test_a_refused_swarm_notice_is_dropped_and_the_quota_step_still_runs(capsys):
    from scripts.swarm.ledger_client import LedgerRefused

    store = _store()
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0, state="running")
    store.create(config)
    ledger = FakeLedger([{"id": "e"}])

    def refuse(*args, **kwargs):
        raise LedgerRefused("ledger sw: server refused: 400 by is allowed only on agent chat and comment entries")

    ledger.comment = refuse
    runtime = FakeRuntime()
    runtime.quota_capacity = lambda cfg, agents, now, demand, requirements: capacity.calculate(
        cfg, [account()], agents, demand
    )
    actions = tick("sw", store, ledger, runtime, 1000)
    assert [task for _, _, task in runtime.spawned] == ["e"]
    assert not any(action.startswith("skipped") for action in actions)
    assert capacity.read(store, "sw")["at"] == 1000
    assert capsys.readouterr().err == (
        "swarm notice dropped, the ledger refused it: "
        "ledger sw: server refused: 400 by is allowed only on agent chat and comment entries\n"
    )


def test_a_missing_signed_in_default_does_not_create_a_fake_token_account(monkeypatch):
    monkeypatch.setattr(balancer, "cached_observations", lambda **kwargs: [])
    monkeypatch.setattr(capacity.account_sessions, "sessions_by_account", lambda: {})
    monkeypatch.setattr(capacity.account_sessions, "codex_sessions_by_account", lambda: {"default": 1})
    monkeypatch.setattr(capacity.codex_router, "routing_pool", lambda env: [])

    def quotas(pool, env):
        assert pool == []
        return {}

    monkeypatch.setattr(capacity.codex_router, "quotas", quotas)
    assert capacity.accounts({}, 100) == []


def test_one_free_seat_goes_to_engineering_before_other_empty_lanes():
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=1, max_plan=1)
    decision = capacity.calculate(config, [account(sessions=2)], [])
    assert decision["effective"] == {"eng": 1, "ci": 0, "plan": 0}


def test_tied_free_seats_break_ties_alphabetically_by_harness():
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0)
    decision = capacity.calculate(config, [account(sessions=2), account("cx", sessions=2, harness="codex")], [])
    assert decision["placements"] == {
        "eng": [{"index": 0, "harness": "claude", "account": "a"}],
        "ci": [],
        "plan": [],
    }


def test_reservations_ignore_fixed_work_beyond_the_configured_lane_cap():
    config = SwarmConfig("sw", "/repo", max_eng=2, max_ci=0, max_plan=0)
    live = [AgentRecord("live", "eng", "active")]
    requirements = {"eng": [("claude", "codex"), ("claude",)], "ci": [], "plan": []}
    seen = [account(sessions=2), account("cx", sessions=2, harness="codex")]
    decision = capacity.calculate(config, seen, live, {"eng": 2, "ci": 0, "plan": 0}, requirements)
    assert decision["placements"] == {
        "eng": [{"index": 0, "harness": "claude", "account": "a"}],
        "ci": [],
        "plan": [],
    }


@pytest.mark.parametrize(
    ("chosen", "task", "expected"),
    [
        ({"profile": "frontend"}, {"id": "e"}, ("claude",)),
        ({"agent": "codex"}, {"id": "e"}, ("codex",)),
        ({}, {"id": "e", "launch_assignment": {"profile": "engineer", "harness": "codex"}}, ("codex",)),
        ({}, {"id": "e", "handoff_envelope": {"launch": {"profile": "frontend", "harness": "codex"}}}, ("claude",)),
    ],
)
def test_requirements_keep_lane_and_saved_profile_constraints(tmp_path, monkeypatch, chosen, task, expected):
    from scripts.swarm import runtime as module

    def requires_claude(profile):
        assert profile in {"frontend", "engineer"}
        return profile == "frontend"

    monkeypatch.setattr(module.plugins, "claude_only", requires_claude)
    runtime = module.HerdrRuntime(home=tmp_path)
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0, lanes={"eng": chosen})
    assert runtime.quota_requirements(config, {"eng": [task], "ci": [], "plan": []})["eng"] == [expected]


def test_capacity_apply_preserves_saved_options_and_controller_evidence(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from scripts.swarm import ledger_client
    from scripts.swarm import runtime as module

    store = _store()
    config = SwarmConfig("sw", "/repo", max_eng=3, max_ci=0, max_plan=0)
    store.create(config)
    tasks = [{"id": "fixed", "profile": "frontend"}, {"id": "saved", "profile": "engineer"}]
    doc = FakeLedger(tasks)
    saved = {"profile": "engineer", "harness": "codex", "model": "sol", "effort": "high"}
    store.redis.hset(store.key("sw", "launch-assignments"), "saved", json.dumps(saved))
    envelope = {"launch": {"profile": "frontend", "harness": "claude"}}

    def handoff(slug, task):
        assert slug == "sw" and task in {"fixed", "saved"}
        return envelope if task == "fixed" else {}

    monkeypatch.setattr(store, "handoff_envelope", handoff)
    rt = module.HerdrRuntime(home=tmp_path)
    monkeypatch.setattr(module.plugins, "claude_only", lambda profile: profile == "frontend")
    original = rt.quota_requirements

    def requirements(cfg, ready):
        assert cfg == config
        assert ready["eng"][0] == {**doc.rows["fixed"], "handoff_envelope": envelope, "launch_assignment": {}}
        assert ready["eng"][1] == {**doc.rows["saved"], "handoff_envelope": {}, "launch_assignment": saved}
        return original(cfg, ready)

    monkeypatch.setattr(rt, "quota_requirements", requirements)

    def observations(env, now, refresh=True):
        assert now == 1234.567
        return [account(sessions=2), account("cx", harness="codex")]

    monkeypatch.setattr(capacity, "accounts", observations)
    calls = []

    def call(slug, ops, service):
        assert slug == "sw" and service is True
        calls.extend(ops)
        return {}

    monkeypatch.setattr(ledger_client, "_ledger", lambda: SimpleNamespace(call=call))
    client = ledger_client.LedgerClient()

    def state(slug):
        assert slug == "sw"
        return doc.state(slug)

    monkeypatch.setattr(client, "state", state)
    result = capacity.apply("sw", config, store, client, rt, 1234567)
    assert capacity.read(store, "sw")["tasks"] == {"fixed": "claude", "saved": "codex"}
    assert capacity.read(store, "sw")["effective"] == {"eng": 2, "ci": 0, "plan": 0}
    assert len(result) == len(calls) == 1
    assert calls[0]["by"] == "swarm"
    assert calls[0]["text"] == notice_text.plain(result[0]) and calls[0]["thread"] == "tasks/fixed/comments"


def test_legacy_runtime_without_quota_reader_performs_no_capacity_work():
    from types import SimpleNamespace

    assert capacity.apply("sw", None, None, None, SimpleNamespace(), 1000) == []


def test_legacy_effective_caps_still_limit_spawns_when_task_mapping_is_absent():
    import json

    from scripts.swarm.tick import _spawn_order

    store = _store()
    config = SwarmConfig("sw", "/repo", max_eng=2, max_ci=1, max_plan=0)
    store.create(config)
    ledger = FakeLedger([{"id": "e"}])
    store.redis.set(store.key("sw", "quota-capacity"), json.dumps({"effective": {"eng": 0, "ci": 0, "plan": 0}}))
    assert _spawn_order("sw", config, store, [], ledger.rows, ledger.state("sw")) == []


def test_lane_harness_pin_wins_over_a_saved_different_harness(tmp_path, monkeypatch):
    from scripts.swarm import runtime as module

    monkeypatch.setattr(module.plugins, "claude_only", lambda profile: False)
    runtime = module.HerdrRuntime(home=tmp_path)
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0, lanes={"eng": {"agent": "codex"}})
    task = {"id": "e", "launch_assignment": {"profile": "engineer", "harness": "claude"}}
    assert runtime.quota_requirements(config, {"eng": [task], "ci": [], "plan": []})["eng"] == [("codex",)]


def test_spawn_obeys_lane_reservations_without_a_task_map(tmp_path, monkeypatch):
    runtime, config, seen = _runtime_probe(tmp_path, monkeypatch, [account(), account("cx", harness="codex")])
    runtime._quota_allocations = {"plan": {"claude": 0, "codex": 1}}
    runtime.spawn(config, "plan", "planner@a1b2c3-0001", {"id": "p", "title": "Plan"})
    assert seen == [("p", "codex", "cx")]


def test_required_profile_keeps_its_forced_choice_when_reservation_agrees(tmp_path, monkeypatch):
    runtime, config, seen = _runtime_probe(tmp_path, monkeypatch, [account()], reason="requested")
    runtime._quota_allocations = {"plan": {"claude": 1, "codex": 0}}
    runtime._quota_tasks = {"p": "claude"}
    placed = runtime.spawn(config, "plan", "planner@a1b2c3-0001", {"id": "p", "title": "Plan", "profile": "frontend"})
    assert placed.choice == "forced"
    assert seen == [("p", "claude", "a")]


@pytest.mark.parametrize("planned", [False, True])
def test_master_affinity_cannot_fall_back_when_its_account_has_no_quota(tmp_path, monkeypatch, planned):
    from scripts.swarm import runtime as module
    from scripts.swarm.tick import SpawnError

    runtime, config, seen = _runtime_probe(tmp_path, monkeypatch, [account(), account("cx", harness="codex", cap=0)])
    monkeypatch.setattr(module.affinity, "desired", lambda cfg: "codex")
    if planned:
        runtime._quota_tasks = {"p": "claude"}
    with pytest.raises(SpawnError, match="^no codex account has placeable quota seats$") as error:
        runtime.spawn(config, "master", "master@a1b2c3-0001", {"id": "p", "title": "Master", "profile": "master"})
    assert error.value.status == "unavailable"
    assert seen == []


def test_capacity_places_ready_tasks_in_the_claim_order():
    store = _store()
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0)
    store.create(config)
    ledger = FakeLedger(
        [
            {"id": "plain"},
            {"id": "deep"},
            {"id": "after", "depends_on": ["deep"], "rank": "low"},
            {"id": "urgent", "rank": "urgent", "lane": "ci"},
        ]
    )
    ledger.state = lambda slug: {"tasks": list(ledger.rows.values())}
    ledger.comment = lambda *args, **kwargs: None
    runtime = FakeRuntime()
    runtime.quota_capacity = lambda cfg, agents, now, demand, requirements: capacity.calculate(
        cfg, [account()], agents, demand
    )
    capacity.apply("sw", config, store, ledger, runtime, 1000)
    assert capacity.read(store, "sw")["tasks"] == {"deep": "claude"}


def test_allocation_skips_a_harness_whose_only_room_is_on_accounts_the_handoff_cannot_take():
    seats = [session_bands.Seat("claude", "a", 1, 0), session_bands.Seat("codex", "x", 5, 0)]
    allocation, placements = capacity._allocate(
        None,
        {"eng": 0, "ci": 0, "plan": 0},
        {"eng": 1, "ci": 1, "plan": 0},
        seats,
        {"eng": [("claude", "codex")], "ci": [("claude",)], "plan": []},
        {"eng": {0: {("claude", "a")}}},
    )
    assert placements == {"eng": [{"index": 0, "harness": "claude", "account": "a"}], "ci": [], "plan": []}


def test_a_warned_account_gets_no_placeable_seat_and_the_reason_names_it():
    config = SwarmConfig("sw", "/repo", max_eng=2, max_ci=0, max_plan=0)
    observed = [account("w", left=5), account("v", left=5), account("cx", harness="codex")]
    result = capacity.calculate(config, observed, [], warned={("claude", "w"): "week", ("claude", "v"): "five hour"})
    assert result["placeable"] == {"claude": 0, "codex": 3}
    assert result["placements"]["eng"] == [
        {"index": 0, "harness": "codex", "account": "cx"},
        {"index": 1, "harness": "codex", "account": "cx"},
    ]
    assert result["accounts"][0]["sessions"] == 0 and result["accounts"][0]["cap"] == 3
    assert result["reason"].endswith(
        "free seats, claude w is at its week quota warning, claude v is at its five hour quota warning"
    )


def test_runtime_capacity_passes_the_warned_accounts_to_the_calculation(tmp_path, monkeypatch):
    from scripts.swarm import runtime as module

    monkeypatch.setattr(capacity, "accounts", lambda env, now, refresh=True: [account("w", left=5), account("ok")])
    rt = module.HerdrRuntime(home=tmp_path)
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0)
    decision = rt.quota_capacity(config, [], 1, {"eng": 1, "ci": 0, "plan": 0})
    assert decision["placements"]["eng"] == [{"index": 0, "harness": "claude", "account": "ok"}]
    assert "claude w is at its week quota warning" in decision["reason"]


def test_a_fresh_spawn_never_lands_on_a_warned_account_while_another_has_a_seat(tmp_path, monkeypatch):
    runtime, config, seen = _runtime_probe(
        tmp_path, monkeypatch, [account("w", left=5), account("cx", harness="codex")]
    )
    runtime.spawn(config, "plan", "planner@a1b2c3-0001", {"id": "p", "title": "Plan"})
    assert seen == [("p", "codex", "cx")]
    assert runtime._rotation("", {}) == ("codex", "rotation")
    assert runtime.has_capacity(config)
    runtime._quota_accounts = [account("w", left=5)]
    assert not runtime.has_capacity(config)


def test_a_claude_only_spawn_with_only_a_warned_account_refuses_naming_it(tmp_path, monkeypatch):
    runtime, config, seen = _runtime_probe(
        tmp_path, monkeypatch, [account("w", left=5), account("v", left=5), account("cx", harness="codex")]
    )
    task = {"id": "p", "title": "Plan", "profile": "frontend"}
    refusal = "no claude account has placeable quota seats: claude w is at its week quota warning; claude v is at its"
    with pytest.raises(SpawnError, match=f"^{refusal} week quota warning$") as error:
        runtime.spawn(config, "plan", "planner@a1b2c3-0001", task)
    assert error.value.status == "unavailable"
    assert seen == []


def test_a_recycle_successor_leaves_its_warned_saved_account_or_refuses_naming_it(tmp_path, monkeypatch):
    saved = {"profile": "planner", "harness": "claude", "model": "fable", "effort": "high", "account": "w"}
    task = {"id": "p", "title": "Continue", "handoff_envelope": {"reason": "recycle", "launch": saved}}
    runtime, config, seen = _runtime_probe(tmp_path, monkeypatch, [account("w", left=5), account("b")])
    runtime.spawn(config, "plan", "planner@a1b2c3-0001", task)
    assert seen == [("p", "claude", "b")]
    runtime, config, seen = _runtime_probe(tmp_path, monkeypatch, [account("w", left=5)])
    with pytest.raises(SpawnError, match="^no claude account has placeable quota seats: claude w is at its week"):
        runtime.spawn(config, "plan", "planner@a1b2c3-0002", task)
    assert seen == []


def _roomy_host():
    from scripts.swarm.host_budget import HostSample

    return HostSample(load1=0.5, cpus=8, available_mb=64_000, agents=2)


def _scaling_runtime(tmp_path, monkeypatch, seen):
    from scripts.swarm import runtime as module

    monkeypatch.setattr(capacity, "accounts", lambda env, now, refresh=True: seen)
    rt = module.HerdrRuntime(home=tmp_path)
    rt.host = _roomy_host
    return rt


def test_a_manual_swarm_keeps_its_caps_and_stores_the_host_decision(tmp_path, monkeypatch):
    from scripts.swarm import host_budget

    seen = [account(cap=6), account("cx", harness="codex", cap=6)]
    rt = _scaling_runtime(tmp_path, monkeypatch, seen)
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0, scaling="manual")
    demand = {"eng": 4, "ci": 1, "plan": 0}
    previous = {
        "autoscale": {"ceilings": {"eng": 9, "ci": 0, "plan": 0}, "pending_raise": {"target": None, "ticks": 0}},
        "host": {"room": 4},
    }
    rt.quota_previous(previous)
    decision = rt.quota_capacity(config, [], 100, demand)
    room = host_budget.spawn_room(config, _roomy_host(), 4)
    expected = {
        **capacity.calculate(config, seen, [], demand, warned={}),
        "host": {"room": room.room, "reason": room.reason, "limit": room.limit, "held": False, "granted_at": 100_000},
    }
    assert json.dumps(decision, sort_keys=True) == json.dumps(expected, sort_keys=True)
    assert "autoscale" not in decision


def test_a_manual_swarm_with_an_unreadable_host_stores_host_unknown(tmp_path, monkeypatch):
    rt = _scaling_runtime(tmp_path, monkeypatch, [account(cap=6)])
    rt.host = lambda: None
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0, scaling="manual")
    decision = rt.quota_capacity(config, [], 100, {"eng": 1, "ci": 0, "plan": 0})
    assert decision["host"] == {
        "room": None,
        "reason": "host unknown: the process files cannot be read, so spawns pass",
        "limit": "unknown",
        "held": False,
        "last": None,
        "granted_at": 100_000,
    }


def test_an_auto_swarm_reads_the_host_once_and_autoscales_on_the_stored_room(tmp_path, monkeypatch):
    readings = [_roomy_host(), None]
    rt = _scaling_runtime(tmp_path, monkeypatch, [account(cap=6)])
    rt.host = lambda: readings.pop(0)
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0, scaling="auto")
    decision = rt.quota_capacity(config, [], 100, {"eng": 1, "ci": 0, "plan": 0})
    assert readings == [None]
    stored = decision["host"]
    assert decision["autoscale"]["host"] == stored
    assert (stored["room"], stored["limit"], stored["granted_at"]) == (46, "load", 100_000)


def test_the_stored_top_level_host_room_wins_over_the_autoscale_copy():
    from scripts.swarm.host_budget import HostSample

    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0)
    inputs = capacity.ScaleInputs(
        [],
        [],
        None,
        lambda: HostSample(load1=10.0, cpus=8, available_mb=64_000, agents=1),
        {"host": {"room": 5}, "autoscale": {"host": {"room": 7}}},
    )
    assert capacity.host_room(config, inputs)["room"] == 5


def test_an_auto_swarm_with_an_unknown_host_scales_on_quota_alone():
    from scripts.swarm import autoscale

    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0, scaling="auto")
    inputs = capacity.fixture_inputs({**_readings(), "previous": {}})
    inputs = capacity.ScaleInputs(inputs.observations, inputs.agents, inputs.demand, lambda: None, {})
    _, decision = capacity.autoscaled(config, inputs)
    free = capacity._placeable(capacity._open(inputs.observations, {}))
    previous = {"ceilings": {"eng": 1, "ci": 0, "plan": 0}, "pending_raise": {"target": None, "ticks": 0}}
    expected = autoscale.calculate(capacity._busy(inputs.agents), free, None, inputs.demand, previous)
    assert decision["pending_raise"] == expected["pending_raise"] == {"target": 13, "ticks": 1}
    assert (
        decision["reason"]
        == "Quota seats 10 (claude 6, codex 4); host room unknown; ceiling 1 of 13; raise held at tick 1 of 3."
    )
    assert decision["host"] == {
        "room": None,
        "reason": "host unknown: the process files cannot be read, so spawns pass",
        "limit": "unknown",
        "held": False,
        "last": None,
        "granted_at": 0,
    }


def test_an_unknown_tick_keeps_the_last_known_room_for_the_band_hold():
    from scripts.swarm.host_budget import HostSample

    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0)
    unknown = capacity.ScaleInputs([], [], None, lambda: None, {"host": {"room": 4, "reason": "r", "limit": "load"}})
    carried = capacity.host_room(config, unknown)
    assert (carried["room"], carried["last"]) == (None, 4)

    def band():
        return HostSample(load1=10.0, cpus=8, available_mb=64_000, agents=2)

    after = capacity.host_room(config, capacity.ScaleInputs([], [], None, band, {"host": carried}))
    assert after == {"room": 4, "reason": after["reason"], "limit": "load", "held": True}
    assert after["reason"] == "one minute load 1.25 per CPU is between the watermarks, the previous room of 4 holds"
    again = capacity.ScaleInputs([], [], None, lambda: None, {"host": carried})
    assert capacity.host_room(config, again)["last"] == 4


def test_a_held_room_keeps_the_time_it_was_first_granted():
    previous = {"host": {"room": 2, "granted_at": 1_000}}
    held = {"room": 2, "reason": "r", "limit": "load", "held": True}
    fresh = {**held, "held": False}
    assert capacity.granted(held, previous, 61_000) == {**held, "granted_at": 1_000}
    assert capacity.granted(fresh, previous, 61_000) == {**fresh, "granted_at": 61_000}
    assert capacity.granted(held, {}, 61_000) == {**held, "granted_at": 61_000}


def _band():
    from scripts.swarm.host_budget import HostSample

    return HostSample(load1=10.0, cpus=8, available_mb=64_000, agents=2)


def _granted(room=3):
    return {"room": room, "reason": "r", "limit": "load", "held": False, "granted_at": 1_000}


def test_repeated_readings_between_the_watermarks_never_raise_the_ceiling_past_the_granted_room():
    config = SwarmConfig("sw", "/repo", max_eng=2, max_ci=0, max_plan=0, scaling="auto")
    seats = [account(cap=6), account("cx", harness="codex", cap=6)]
    idle = {"target": None, "ticks": 0}
    previous = {"host": _granted(), "autoscale": {"ceilings": {"eng": 2, "ci": 0, "plan": 0}, "pending_raise": idle}}
    live, asked, totals = 2, [], []
    for reading in range(12):
        agents = [AgentRecord(f"eng-{n}", "eng", "") for n in range(live)]
        inputs = capacity.ScaleInputs(
            seats,
            agents,
            {"eng": 9, "ci": 0, "plan": 0},
            _band,
            previous,
            spent=lambda since_ms, n=live - 2: asked.append(since_ms) or n,
            now_ms=61_000 + reading * 60_000,
        )
        _, decision = capacity.autoscaled(config, inputs)
        assert decision["host"] == {**_granted(), "held": True, "reason": decision["host"]["reason"]}
        totals.append(sum(decision["ceilings"].values()))
        previous = {"host": decision["host"], "autoscale": decision}
        live += live < totals[-1]
    assert totals == [2, 2, 4] + [5] * 9
    assert asked == [1_000] * 12


def test_the_unspent_room_never_drops_below_zero_and_an_unknown_room_stays_unknown():
    calls = []
    assert capacity.unspent({"room": 2, "granted_at": 7}, lambda since_ms: calls.append(since_ms) or 5) == 0
    assert capacity.unspent({"room": 4, "granted_at": 8}, lambda since_ms: calls.append(since_ms) or 1) == 3
    assert capacity.unspent({"room": None, "granted_at": 9}, lambda since_ms: calls.append(since_ms) or 0) is None
    assert calls == [7, 8]


def test_scale_inputs_default_to_no_spawns_at_time_zero():
    inputs = capacity.ScaleInputs([], [], None, lambda: None, {})
    assert (inputs.spent, inputs.now_ms) == (capacity.no_spawns, 0)
    assert capacity.no_spawns(5_000) == 0


def test_quota_capacity_autoscales_on_the_room_left_since_it_was_first_granted(tmp_path, monkeypatch):
    rt = _scaling_runtime(tmp_path, monkeypatch, [account(cap=6), account("cx", harness="codex", cap=6)])
    rt.host = _band
    asked = []
    rt.quota_spent(lambda since_ms: asked.append(since_ms) or 2)
    idle = {"target": None, "ticks": 0}
    rt.quota_previous(
        {"host": _granted(), "autoscale": {"ceilings": {"eng": 4, "ci": 0, "plan": 0}, "pending_raise": idle}}
    )
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0, scaling="auto")
    agents = [AgentRecord(f"eng-{n}", "eng", "") for n in range(4)]
    decision = rt.quota_capacity(config, agents, 100, {"eng": 9, "ci": 0, "plan": 0})
    assert asked == [1_000]
    assert decision["host"] == {**_granted(), "held": True, "reason": decision["host"]["reason"]}
    assert decision["autoscale"]["host"] == decision["host"]
    assert decision["autoscale"]["pending_raise"] == {"target": 5, "ticks": 1}


def test_a_runtime_without_a_spawn_counter_autoscales_on_the_whole_room(tmp_path, monkeypatch):
    rt = _scaling_runtime(tmp_path, monkeypatch, [account(cap=6), account("cx", harness="codex", cap=6)])
    rt.host = _band
    idle = {"target": None, "ticks": 0}
    rt.quota_previous(
        {"host": _granted(), "autoscale": {"ceilings": {"eng": 4, "ci": 0, "plan": 0}, "pending_raise": idle}}
    )
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0, scaling="auto")
    agents = [AgentRecord(f"eng-{n}", "eng", "") for n in range(4)]
    decision = rt.quota_capacity(config, agents, 100, {"eng": 9, "ci": 0, "plan": 0})
    assert decision["autoscale"]["pending_raise"] == {"target": 7, "ticks": 1}


def test_apply_hands_the_runtime_the_spawn_count_the_host_gate_reads(tmp_path, monkeypatch):
    from scripts.swarm import tick as tick_module

    store = _store()
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0, scaling="auto"))
    ledger = FakeLedger([{"id": "e0"}])
    ledger.comment = lambda slug, item, text, by: None
    rt = _scaling_runtime(tmp_path, monkeypatch, [account(cap=6)])
    counters = []
    rt.quota_spent = counters.append
    for name, at in (("lagged", 31_000), ("now", 61_000), ("later", 61_001)):
        tick_module._spend_host(store, name, at)
    _scaling_tick(store, ledger, rt, 61_000)
    (counter,) = counters
    assert (counter(61_000), counter(61_001)) == (2, 1)


def _scaling_tick(store, ledger, rt, now_ms):
    capacity.apply("sw", store.config("sw"), store, ledger, rt, now_ms)
    return capacity.read(store, "sw")


def test_an_auto_swarm_with_quota_and_host_room_rises_above_its_configured_caps_after_a_held_raise(
    tmp_path, monkeypatch
):
    store = _store()
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0, scaling="auto"))
    ledger = FakeLedger([{"id": f"e{n}"} for n in range(4)] + [{"id": "c", "lane": "ci"}])
    ledger.comment = lambda slug, item, text, by: None
    rt = _scaling_runtime(tmp_path, monkeypatch, [account(cap=6), account("cx", harness="codex", cap=6)])
    first = _scaling_tick(store, ledger, rt, 1000)
    assert sum(first["configured"].values()) == 1
    assert first["autoscale"]["pending_raise"] == {"target": 12, "ticks": 1}
    second = _scaling_tick(store, ledger, rt, 61_000)
    assert second["autoscale"]["pending_raise"] == {"target": 12, "ticks": 2}
    assert "raise held at tick 2 of 3" in second["autoscale"]["reason"]
    third = _scaling_tick(store, ledger, rt, 121_000)
    assert third["configured"] == {"eng": 2, "ci": 1, "plan": 0}
    assert third["effective"] == {"eng": 2, "ci": 1, "plan": 0}
    assert third["autoscale"]["host"]["room"] > 0
    assert store.config("sw").max_eng == 1


def test_an_auto_swarm_lowers_its_ceiling_at_once_when_quota_drains(tmp_path, monkeypatch):
    store = _store()
    store.create(SwarmConfig("sw", "/repo", max_eng=4, max_ci=1, max_plan=0, scaling="auto"))
    ledger = FakeLedger([{"id": f"e{n}"} for n in range(4)])
    ledger.comment = lambda slug, item, text, by: None
    rt = _scaling_runtime(tmp_path, monkeypatch, [account(cap=1)])
    decision = _scaling_tick(store, ledger, rt, 1000)
    assert decision["configured"] == {"eng": 1, "ci": 0, "plan": 0}
    assert decision["autoscale"]["pending_raise"] == {"target": None, "ticks": 0}


def test_an_auto_swarm_holds_its_previous_host_room_between_the_watermarks(tmp_path, monkeypatch):
    from scripts.swarm.host_budget import HostSample

    store = _store()
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0, scaling="auto"))
    ledger = FakeLedger([{"id": f"e{n}"} for n in range(4)])
    ledger.comment = lambda slug, item, text, by: None
    rt = _scaling_runtime(tmp_path, monkeypatch, [account(cap=6)])
    first = _scaling_tick(store, ledger, rt, 1000)["autoscale"]["host"]["room"]
    assert first > 0
    rt.host = lambda: HostSample(load1=10.0, cpus=8, available_mb=64_000, agents=2)
    second = _scaling_tick(store, ledger, rt, 61_000)["autoscale"]["host"]
    assert second["room"] == first
    assert "previous room" in second["reason"]


def test_the_autoscale_command_reads_a_live_swarm_without_refreshing_quota(capsys, monkeypatch):
    from scripts.swarm import cli, host_budget

    store = _store()
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0))
    stored = {"autoscale": {"ceilings": {"eng": 1, "ci": 0, "plan": 0}, "pending_raise": {"target": 12, "ticks": 2}}}
    store.redis.set(store.key("sw", "quota-capacity"), json.dumps(stored))
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setattr(cli, "LedgerClient", lambda: FakeLedger([{"id": f"e{n}"} for n in range(4)]))
    refreshes = []
    observed = [account(cap=6), account("cx", harness="codex", cap=6)]
    monkeypatch.setattr(capacity, "accounts", lambda env, now, refresh=True: refreshes.append(refresh) or observed)
    monkeypatch.setattr(host_budget, "read_host", _roomy_host)
    cli.main(["sw", "autoscale", "--json"])
    printed = json.loads(capsys.readouterr().out)
    assert refreshes == [False]
    assert printed["ceilings"]["eng"] > 1
    assert json.loads(store.redis.get(store.key("sw", "quota-capacity"))) == stored


def test_the_autoscale_command_prints_the_decision_for_a_fixture(tmp_path, capsys, monkeypatch):
    from scripts.swarm import cli

    store = _store()
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0))
    monkeypatch.setattr(cli, "connect", lambda: store)
    fixture = tmp_path / "readings.json"
    fixture.write_text(
        json.dumps(
            {
                "accounts": [account(cap=6).__dict__, account("cx", harness="codex", cap=6).__dict__],
                "host": {"load1": 0.5, "cpus": 8, "available_mb": 64_000, "agents": 2},
                "live": {"eng": 0, "ci": 0, "plan": 0},
                "demand": {"eng": 4, "ci": 1, "plan": 0},
                "previous": {
                    "autoscale": {
                        "ceilings": {"eng": 1, "ci": 0, "plan": 0},
                        "pending_raise": {"target": 12, "ticks": 2},
                    }
                },
            }
        )
    )
    cli.main(["sw", "autoscale", "--fixture", str(fixture), "--json"])
    printed = json.loads(capsys.readouterr().out)
    assert (printed["scaling"], printed["applied"]) == ("auto", True)
    assert printed["ceilings"] == {"plan": 0, "ci": 1, "eng": 2}
    assert printed["host"]["room"] > 0
    cli.main(["sw", "autoscale", "--fixture", str(fixture)])
    text = capsys.readouterr().out
    assert "scaling auto, ceilings eng 2, ci 1, plan 0" in text
    assert "host room" in text
    assert store.redis.get(store.key("sw", "quota-capacity")) is None


def test_the_autoscale_command_previews_a_manual_swarm_without_applying_it(tmp_path, capsys, monkeypatch):
    from scripts.swarm import cli

    store = _store()
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0, scaling="manual"))
    monkeypatch.setattr(cli, "connect", lambda: store)
    fixture = tmp_path / "readings.json"
    fixture.write_text(json.dumps(_readings()))
    cli.main(["sw", "autoscale", "--fixture", str(fixture), "--json"])
    printed = json.loads(capsys.readouterr().out)
    assert (printed["scaling"], printed["applied"]) == ("manual", False)
    assert printed["ceilings"] == {"eng": 2, "ci": 1, "plan": 0}
    assert store.config("sw").scaling == "manual"
    assert store.redis.get(store.key("sw", "quota-capacity")) is None


def test_configured_reads_each_lane_cap_in_lane_order():
    config = SwarmConfig("sw", "/repo", max_eng=3, max_ci=2, max_plan=1)
    assert capacity._configured(config) == {"eng": 3, "ci": 2, "plan": 1}


def _readings():
    return {
        "accounts": [account(cap=6).__dict__, account("cx", harness="codex", cap=4).__dict__],
        "host": {"load1": 9.6, "cpus": 8, "available_mb": 5_000, "agents": 1},
        "live": {"eng": 2, "ci": 1},
        "demand": {"eng": 4, "ci": 1, "plan": 0},
        "previous": {
            "autoscale": {"ceilings": {"eng": 2, "ci": 1, "plan": 0}, "pending_raise": {"target": 9, "ticks": 1}}
        },
    }


def test_fixture_inputs_carry_every_reading_and_default_the_optional_ones():
    from scripts.swarm.host_budget import HostSample

    readings = _readings()
    inputs = capacity.fixture_inputs(readings)
    assert inputs.observations == [account(cap=6), account("cx", harness="codex", cap=4)]
    assert inputs.agents == [
        AgentRecord("eng-0", "eng", ""),
        AgentRecord("eng-1", "eng", ""),
        AgentRecord("ci-0", "ci", ""),
    ]
    assert inputs.demand == {"eng": 4, "ci": 1, "plan": 0}
    assert inputs.host() == HostSample(load1=9.6, cpus=8, available_mb=5_000, agents=1)
    assert inputs.previous == readings["previous"]
    assert inputs.warned == {}
    bare = capacity.fixture_inputs({"accounts": [], "host": readings["host"]})
    assert (bare.observations, bare.agents, bare.demand, bare.previous) == ([], [], None, {})


def test_autoscaled_uses_the_swarm_watermarks_and_the_stored_state():
    from scripts.swarm import autoscale, host_budget

    config = SwarmConfig(
        "sw", "/repo", max_eng=1, max_ci=0, max_plan=5, load_high=2.0, load_low=1.5, memory_per_agent_mb=1000
    )
    inputs = capacity.fixture_inputs(
        {**_readings(), "host": {"load1": 0, "cpus": 8, "available_mb": 5000, "agents": 0}}
    )
    scaled, decision = capacity.autoscaled(config, inputs)
    room = host_budget.room(inputs.host(), host_budget.Thresholds(2.0, 1.5, 1000), None)
    previous = {"ceilings": {"eng": 2, "ci": 1, "plan": 0}, "pending_raise": {"target": 9, "ticks": 1}}
    free = capacity._placeable(capacity._open(inputs.observations, {}))
    expected = autoscale.calculate(capacity._busy(inputs.agents), free, room.room, inputs.demand, previous)
    assert decision == {
        **expected,
        "host": {"room": room.room, "reason": room.reason, "limit": room.limit, "held": room.held, "granted_at": 0},
    }
    assert "below the low watermark" in room.reason
    caps = decision["ceilings"]
    assert (scaled.max_eng, scaled.max_ci, scaled.max_plan) == (caps["eng"], caps["ci"], caps["plan"])
    assert (scaled.slug, scaled.load_high) == ("sw", 2.0)


def test_autoscaled_seeds_from_the_configured_caps_and_an_idle_raise():
    from scripts.swarm import autoscale, host_budget

    config = SwarmConfig("sw", "/repo", max_eng=2, max_ci=1, max_plan=0)
    inputs = capacity.fixture_inputs(
        {
            **_readings(),
            "previous": {},
            "demand": None,
            "live": {},
            "host": {"load1": 0, "cpus": 8, "available_mb": 64000, "agents": 0},
        }
    )
    _, decision = capacity.autoscaled(config, inputs)
    room = host_budget.room(inputs.host(), host_budget.Thresholds(), None)
    previous = {"ceilings": {"eng": 2, "ci": 1, "plan": 0}, "pending_raise": {"target": None, "ticks": 0}}
    free = capacity._placeable(capacity._open(inputs.observations, {}))
    zero = {"eng": 0, "ci": 0, "plan": 0}
    expected = autoscale.calculate(capacity._busy(inputs.agents), free, room.room, zero, previous)
    assert decision == {
        **expected,
        "host": {"room": room.room, "reason": room.reason, "limit": room.limit, "held": room.held, "granted_at": 0},
    }


def test_autoscaled_passes_the_stored_host_room_on():
    readings = {
        **_readings(),
        "previous": {"autoscale": {"host": {"room": 7}}},
        "host": {"load1": 10.0, "cpus": 8, "available_mb": 64_000, "agents": 1},
    }
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0)
    _, decision = capacity.autoscaled(config, capacity.fixture_inputs(readings))
    assert decision["host"]["room"] == 7


def test_live_inputs_read_quota_without_a_refresh_and_count_ready_demand(monkeypatch):
    from scripts.swarm import host_budget

    store = _store()
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0))
    stored = {"autoscale": {"ceilings": {"eng": 1, "ci": 0, "plan": 0}}}
    store.redis.set(store.key("sw", "quota-capacity"), json.dumps(stored))
    store.put_agent("sw", AgentRecord("engineer@x-0001", "eng", "e0", state="working"))
    ledger = FakeLedger([{"id": f"e{n}"} for n in range(3)] + [{"id": "c", "lane": "ci"}])
    state = ledger.state
    slugs = []
    monkeypatch.setattr(ledger, "state", lambda slug: slugs.append(slug) or state(slug))
    seen = []
    observed = [account(cap=6), account("warned", cap=6, left=1)]
    monkeypatch.setattr(
        capacity, "accounts", lambda env, now, refresh=True: seen.append((env, now, refresh)) or observed
    )
    inputs = capacity.live_inputs("sw", store, ledger, {"X": "1"}, 5_000)
    assert slugs == ["sw"]
    assert seen == [({"X": "1"}, 5.0, False)]
    assert inputs.observations == observed
    assert [agent.name for agent in inputs.agents] == ["engineer@x-0001"]
    assert inputs.demand == {"eng": 3, "ci": 1, "plan": 0}
    assert inputs.host is host_budget.read_host
    assert inputs.previous == stored
    assert inputs.warned == {("claude", "warned"): "week"}
    from scripts.swarm.tick import _spend_host

    _spend_host(store, "now", 5_000)
    _spend_host(store, "after", 5_001)
    assert (inputs.now_ms, inputs.spent(5_000)) == (5_000, 1)


def test_autoscale_lines_name_the_mode_the_raise_the_room_and_the_reason():
    from scripts.swarm import cli

    decision = {
        "ceilings": {"eng": 4, "ci": 1, "plan": 0},
        "pending_raise": {"target": 6, "ticks": 2},
        "host": {"room": 3, "reason": "roomy"},
        "reason": "quota allows six",
    }
    manual = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0, scaling="manual")
    assert cli.autoscale_lines(manual, decision) == [
        "scaling manual, ceilings eng 4, ci 1, plan 0",
        "manual scaling keeps the configured caps eng 1, ci 0, plan 0",
        "pending raise to 6, held 2 of 3 ticks",
        "host room 3: roomy",
        "quota allows six",
    ]
    idle = {**decision, "pending_raise": {"target": None, "ticks": 0}}
    assert cli.autoscale_lines(replace(manual, scaling="auto"), idle) == [
        "scaling auto, ceilings eng 4, ci 1, plan 0",
        "host room 3: roomy",
        "quota allows six",
    ]


def test_the_autoscale_command_prints_exact_output_for_an_unknown_swarm(tmp_path, capsys, monkeypatch):
    from scripts.swarm import cli

    store = _store()
    monkeypatch.setattr(cli, "connect", lambda: store)
    fixture = tmp_path / "readings.json"
    unknown = SwarmConfig("new", "", max_eng=0, max_ci=0)
    readings = {
        **_readings(),
        "previous": {},
        "live": {},
        "host": {"load1": 0, "cpus": 8, "available_mb": 64000, "agents": 0},
    }
    fixture.write_text(json.dumps(readings))
    _, decision = capacity.autoscaled(unknown, capacity.fixture_inputs(readings))
    cli.main(["new", "autoscale", "--fixture", str(fixture), "--json"])
    expected = json.dumps({"scaling": "auto", "applied": True, **decision}, indent=2)
    assert capsys.readouterr().out == expected + "\n"
    cli.main(["new", "autoscale", "--fixture", str(fixture)])
    lines = cli.autoscale_lines(unknown, decision)
    assert capsys.readouterr().out == "\n".join(lines) + "\n"
    parsed = cli.build_parser().parse_args(["new", "autoscale"])
    assert (parsed.fixture, parsed.json) == ("", False)


def test_quota_capacity_hands_autoscale_its_previous_state_and_warnings(tmp_path, monkeypatch):
    from scripts.swarm import runtime as module

    seen = []
    observed = [account(cap=6), account("warned", cap=6, left=1)]
    monkeypatch.setattr(capacity, "accounts", lambda env, now, refresh=True: observed)
    monkeypatch.setattr(capacity, "autoscaled", lambda config, inputs, host: seen.append(inputs) or (config, None))
    rt = module.HerdrRuntime(home=tmp_path)
    previous = {"autoscale": {"ceilings": {"eng": 2, "ci": 0, "plan": 0}}}
    rt.quota_previous(previous)
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0)
    demand = {"eng": 1, "ci": 0, "plan": 0}
    rt.quota_capacity(config, [], 100, demand)
    warned = {("claude", "warned"): "week"}
    assert seen == [capacity.ScaleInputs(observed, [], demand, rt.host, previous, warned, capacity.no_spawns, 100_000)]


def api(harness="claude", weight=25, sessions=0, cap=10**6):
    return capacity.Account(harness, "api", capacity._state(cap), sessions, None, None, cap, kind="api", weight=weight)


def _api_policy(monkeypatch, weight=25, cap=10**6):
    from scripts.routing import place

    monkeypatch.setattr(capacity.place, "policy", lambda harness, environ: place.ApiPolicy(weight, cap))


def test_live_api_sessions_count_on_the_api_row_only_and_keep_accounts_known(monkeypatch):
    _api_policy(monkeypatch)
    monkeypatch.setattr(balancer, "cached_observations", lambda **kw: [])
    monkeypatch.setattr(capacity.account_sessions, "sessions_by_account", lambda: {"api": 2})
    monkeypatch.setattr(capacity.account_sessions, "codex_sessions_by_account", lambda: {"api": 1})
    monkeypatch.setattr(capacity.codex_router.CodexAccountSource, "pool", lambda self, env: [])
    env = {"ANTHROPIC_API_KEY": "fake", "OPENAI_API_KEY": "fake"}
    rows = capacity.accounts(env, 100)
    assert rows == [api(sessions=2), api("codex", sessions=1)]
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0)
    assert "unknown" not in capacity.calculate(config, rows, [])["reason"]


def test_live_api_sessions_without_an_api_endpoint_show_as_a_closed_api_row(monkeypatch):
    monkeypatch.setattr(balancer, "cached_observations", lambda **kw: [])
    monkeypatch.setattr(capacity.account_sessions, "sessions_by_account", lambda: {"api": 1})
    monkeypatch.setattr(capacity.codex_router, "routing_pool", lambda env: [])
    assert capacity.accounts({}, 100) == [
        capacity.Account("claude", "api", "CLOSED", 1, None, None, 0, kind="api"),
    ]


def _api_share(weight, placed=8):
    config = SwarmConfig("sw", "/repo", max_eng=placed, max_ci=0, max_plan=0)
    rows = [account(name, cap=6) for name in ("a", "b", "c")] + [api(weight=weight)]
    return [slot["account"] for slot in capacity.calculate(config, rows, [])["placements"]["eng"]]


def test_api_at_weight_zero_takes_lane_seats_only_when_the_pool_is_full():
    assert "api" not in _api_share(0)
    config = SwarmConfig("sw", "/repo", max_eng=2, max_ci=0, max_plan=0)
    decision = capacity.calculate(config, [account("a", cap=1), api(weight=0)], [])
    assert [slot["account"] for slot in decision["placements"]["eng"]] == ["a", "api"]


def test_api_at_weight_twenty_five_takes_one_in_four_lane_seats():
    assert _api_share(25).count("api") == 2


def test_api_at_weight_one_hundred_takes_every_lane_seat():
    assert _api_share(100) == ["api"] * 8


def test_capacity_reason_counts_pool_seats_and_names_the_api_weight():
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0)
    reason = capacity.calculate(config, [account("a"), api()], [])["reason"]
    assert (
        reason
        == "accounts have quota; Claude has 3 free seats and Codex has 0 free seats; Claude api is open at weight 25"
    )


def test_a_closed_api_row_never_marks_the_accounts_restricted():
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0)
    closed = capacity.Account("claude", "api", "CLOSED", 1, None, None, 0, kind="api")
    assert capacity.calculate(config, [account("a"), closed], [])["reason"].startswith("accounts have quota;")


def test_a_lane_restricted_to_some_accounts_splits_on_every_live_session():
    offered = capacity.offered([account("a", cap=6, sessions=5), account("b", cap=6), api(sessions=1)])
    assert capacity.pick(offered).account == "api"
    assert capacity.pick(offered, lambda seat: seat.account in {"b", "api"}).account == "api"
    assert capacity.pick(offered, lambda seat: seat.account == "b").account == "b"


def test_an_observed_share_at_the_weight_still_sends_the_next_seat_to_the_api():
    from scripts.swarm import quota_view

    rows = [account("a", cap=6, sessions=3), api(sessions=1)]
    decision = {"accounts": [capacity.record(row) for row in rows]}
    assert quota_view.api_share(decision["accounts"][1], decision["accounts"]) == (25, 4)
    assert capacity.pick(capacity.offered(rows)).account == "api"


def test_accounts_ask_each_harness_api_side_with_the_environment_and_time(monkeypatch):
    calls = []

    def api_side(source, harness, environ, now):
        calls.append((type(source).__name__, harness, environ, now))
        return [], 0

    monkeypatch.setattr(capacity.place, "api_side", api_side)
    monkeypatch.setattr(balancer, "cached_observations", lambda **kw: [])
    monkeypatch.setattr(capacity.account_sessions, "sessions_by_account", lambda: {})
    monkeypatch.setattr(capacity.codex_router, "routing_pool", lambda env: [])
    capacity.accounts({"A": "1"}, 100)
    assert calls == [
        ("ClaudeApiSource", "claude", {"A": "1"}, 100),
        ("CodexApiSource", "codex", {"A": "1"}, 100),
    ]


def test_allocation_honours_the_account_restriction_of_each_lane_seat():
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0)
    rows = [account("a"), account("b", sessions=1)]
    decision = capacity.calculate(config, rows, [], accounts={"eng": {0: {("claude", "b")}}})
    assert decision["placements"]["eng"] == [{"index": 0, "harness": "claude", "account": "b"}]


def test_reason_names_every_api_side_and_a_closed_one_without_a_weight():
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0)
    closed = capacity.Account("codex", "api", "CLOSED", 1, None, None, 0, kind="api")
    assert capacity.calculate(config, [account("a"), api(), closed], [])["reason"].endswith(
        "; Claude api is open at weight 25; Codex api is closed"
    )


def test_an_account_at_its_quota_warning_never_takes_the_spawn_pick(tmp_path, monkeypatch):
    runtime, config, seen = _runtime_probe(tmp_path, monkeypatch, [account("w", left=5), account("b", sessions=1)])
    assert runtime._quota_account("claude", None, None).name == "b"


def test_sessions_on_full_accounts_weigh_in_the_api_share_of_a_spawn(tmp_path, monkeypatch):
    rows = [account("full", cap=4, sessions=4), account("a", cap=6), api(sessions=1)]
    runtime, config, seen = _runtime_probe(tmp_path, monkeypatch, rows)
    assert runtime._quota_account("claude", None, None).name == "api"
    runtime._quota_accounts = rows[1:]
    assert runtime._quota_account("claude", None, None).name == "a"


def test_rotation_follows_the_api_split():
    from scripts.swarm import runtime as module

    runtime = module.HerdrRuntime(home=None, choose=lambda requested, env: ("claude", "priority"))
    runtime._quota_accounts = [account("a", sessions=2, cap=6), account("cx", 6, 3, harness="codex"), api("codex", 0)]
    assert runtime._rotation("", {}) == ("claude", "rotation")


def test_an_api_account_round_trips_through_the_route_argument(tmp_path, monkeypatch):
    runtime, config, seen = _runtime_probe(tmp_path, monkeypatch, [account("a", cap=0), api(weight=0)])
    runtime.spawn(config, "plan", "planner@a1b2c3-0001", {"id": "p", "title": "Plan"})
    assert seen == [("p", "claude", "api")]
    assert runtime._quota_accounts[-1].sessions == 1
    agent = AgentRecord("planner@a1b2c3-0001", "plan", "p", harness="claude", account="api", profile="planner")
    agent = replace(agent, conversation_id="c1")
    seen.clear()
    monkeypatch.setattr(runtime, "_holds", lambda pane, conversation: True)
    runtime.resume(config, agent, "text")
    assert seen == [("p", "claude", "api")]
