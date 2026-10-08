from dataclasses import replace

import pytest

from scripts import claude_quota_balancer as balancer
from scripts.swarm import capacity
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeLedger, FakeRuntime

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture(autouse=True)
def isolated_accounts(monkeypatch):
    monkeypatch.setattr(capacity.session_caps, "stored", lambda harness: {})
    monkeypatch.setattr(capacity.account_sessions, "codex_sessions_by_account", lambda: {})


def _store():
    import fakeredis

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


def account(name="a", state="NORMAL", sessions=0, left=90, harness="claude"):
    return capacity.Account(harness, name, state, sessions, left, left)


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


def test_two_draining_accounts_lower_caps_and_reset_restores_them():
    config = SwarmConfig("sw", "/repo", max_eng=3, max_ci=2, max_plan=1)
    drained = [account("a", "DRAIN", left=4), account("b", "DRAIN", left=2)]
    low = capacity.calculate(config, drained, [], 3, 5)
    assert low["effective"] == {"eng": 0, "ci": 0, "plan": 0}
    assert low["placeable"] == {"claude": 0, "codex": 0}
    high = capacity.calculate(config, [account("a"), account("b")], [], 3, 5)
    assert high["effective"] == {"eng": 3, "ci": 2, "plan": 1}


@pytest.mark.parametrize(
    ("state", "left", "sessions", "expected"),
    [
        ("NORMAL", 90, 0, 4),
        ("REDUCE", 30, 0, 2),
        ("REDUCE", 30, 2, 0),
        ("DRAIN_SOON", 20, 0, 4),
        ("DRAIN_SOON", 19, 0, 0),
        ("DRAIN", 10, 0, 0),
        ("UNKNOWN", None, 0, 0),
        ("BLOCKED", 0, 0, 0),
    ],
)
def test_placeable_seats_obey_state_and_occupancy(state, left, sessions, expected):
    assert capacity.free_seats(account(state=state, left=left, sessions=sessions), 4, 5) == expected


def test_codex_week_floor_and_explicit_lane_harness():
    config = SwarmConfig(
        "sw", "/repo", max_eng=2, max_ci=2, max_plan=1, lanes={"eng": {"agent": "claude"}, "ci": {"agent": "codex"}}
    )
    result = capacity.calculate(config, [account(), account("cx", harness="codex", left=4)], [], 3, 5)
    assert result["effective"] == {"eng": 2, "ci": 0, "plan": 1}


def test_one_seat_goes_to_the_empty_lane_before_another_engineer():
    config = SwarmConfig("sw", "/repo", max_eng=2, max_ci=1, max_plan=1)
    agents = [AgentRecord("engineer", "eng", "e", harness="claude")]
    result = capacity.calculate(config, [account(sessions=2)], agents, 3, 5)
    assert result["effective"] == {"eng": 1, "ci": 1, "plan": 0}


def test_reduced_caps_do_not_retire_work_and_changes_are_recorded_once(monkeypatch):
    store = _store()
    config = SwarmConfig("sw", "/repo", max_eng=2, max_ci=1, max_plan=0, state="running")
    store.create(config)
    ledger = FakeLedger([{"id": "e"}, {"id": "e2"}, {"id": "c", "lane": "ci"}])
    ledger.comments = []
    ledger.comment = lambda slug, item, text, by: ledger.comments.append((slug, item, text, by))
    runtime = FakeRuntime()
    monkeypatch.setattr(capacity, "accounts", lambda env, now: [account(sessions=1)])
    runtime.quota_capacity = lambda cfg, agents, now, demand, requirements: capacity.calculate(
        cfg, capacity.accounts({}, now), agents, 3, 5, demand
    )
    tick("sw", store, ledger, runtime, 1000)
    agents = store.agents("sw")
    assert len(runtime.spawned) == 2
    monkeypatch.setattr(capacity, "accounts", lambda env, now: [account(state="DRAIN", left=3)])
    actions = tick("sw", store, ledger, runtime, 2000)
    assert not runtime.killed
    assert capacity.read(store, "sw")["effective"] == {"eng": 1, "ci": 1, "plan": 0}
    assert len(ledger.comments) == 2
    assert len([a for a in actions if a.startswith("quota capacity")]) == 1
    tick("sw", store, ledger, runtime, 3000)
    assert len(ledger.comments) == 2
    assert store.config("sw").max_eng == 2
    assert [a.name for a in store.agents("sw")] == [a.name for a in agents]
    monkeypatch.setattr(capacity, "accounts", lambda env, now: [account()])
    tick("sw", store, ledger, runtime, 4000)
    assert capacity.read(store, "sw")["effective"] == {"eng": 2, "ci": 1, "plan": 0}
    assert len(ledger.comments) == 3


def test_automatic_harness_falls_through_to_a_placeable_account(tmp_path, monkeypatch):
    from scripts.swarm import runtime as module

    rt = module.HerdrRuntime(home=tmp_path, choose=lambda *_: ("claude", "priority"))
    rt._quota_accounts = [account(state="DRAIN", left=4), account("cx", harness="codex")]
    rt._quota_cap, rt._quota_floor, rt._quota_share = 3, 5, 30
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


def test_reset_changes_account_state_from_drain_to_normal(monkeypatch):
    windows = balancer.QuotaWindow(used=96, resets_at=200)
    result = balancer.ProbeResult("a", "allowed", "DRAIN", 4, windows, windows)
    monkeypatch.setattr(balancer, "cached_observations", lambda **kw: [(100, result)])
    monkeypatch.setattr(capacity.account_sessions, "sessions_by_account", lambda: {})
    monkeypatch.setattr(capacity.codex_router, "routing_pool", lambda _: [])
    assert capacity.accounts({}, 199)[0].state == "DRAIN"
    assert capacity.accounts({}, 200)[0] == account(left=100)


def test_effective_caps_are_exposed_in_status(monkeypatch):
    from scripts.swarm import status

    store = _store()
    store.create(SwarmConfig("sw", "/repo", max_eng=2, max_ci=1))
    decision = {"effective": {"eng": 0, "ci": 0, "plan": 0}, "reason": "accounts are drain"}
    store.redis.set(
        store.key("sw", "quota-capacity"), '{"effective":{"eng":0,"ci":0,"plan":0},"reason":"accounts are drain"}'
    )
    monkeypatch.setattr(status, "page_quota", lambda: {})
    assert status.status_report(store, "sw", {"tasks": []})["quota_capacity"] == decision
    assert capacity.status_line(decision) == "quota capacity eng 0 ci 0 plan 0 because accounts are drain"


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
    result = capacity.calculate(config, [account(sessions=2)], [], 3, 5, demand={"eng": 0, "ci": 1, "plan": 0})
    assert result["effective"] == {"eng": 0, "ci": 1, "plan": 0}


def test_automatic_lanes_preserve_seats_required_by_fixed_lanes():
    config = SwarmConfig("sw", "/repo", max_eng=2, max_ci=2, max_plan=0, lanes={"ci": {"agent": "claude"}})
    result = capacity.calculate(config, [account(sessions=1), account("cx", sessions=1, harness="codex")], [], 3, 5)
    assert result["effective"] == {"eng": 2, "ci": 2, "plan": 0}
    assert result["allocation"] == {
        "eng": {"claude": 0, "codex": 2},
        "ci": {"claude": 2, "codex": 0},
        "plan": {"claude": 0, "codex": 0},
    }


def test_failed_capacity_comment_is_retried_without_losing_the_decision():
    store = _store()
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0)
    store.create(config)
    ledger = FakeLedger([{"id": "e"}])
    runtime = FakeRuntime()
    runtime.quota_capacity = lambda cfg, agents, now, demand, requirements: capacity.calculate(
        cfg, [account()], agents, 3, 5, demand
    )
    ledger.comment = lambda *args, **kw: (_ for _ in ()).throw(RuntimeError("ledger unavailable"))
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        capacity.apply("sw", config, store, ledger, runtime, 1000)
    assert capacity.read(store, "sw") == {}
    comments = []
    ledger.comment = lambda *args, **kw: comments.append((args, kw))
    assert len(capacity.apply("sw", config, store, ledger, runtime, 2000)) == 1
    assert len(comments) == 1


def test_codex_accounts_with_live_sessions_keep_their_own_quotas(monkeypatch):
    from scripts.codex_quota import CodexQuota

    monkeypatch.setattr(balancer, "cached_observations", lambda **kw: [])
    monkeypatch.setattr(capacity.account_sessions, "sessions_by_account", lambda: {})
    monkeypatch.setattr(capacity.account_sessions, "codex_sessions_by_account", lambda: {"a": 1, "b": 2})
    monkeypatch.setattr(
        capacity.codex_router, "routing_pool", lambda _: [capacity.codex_router.CodexAccount("a", "AH_CX_TOKEN_a")]
    )

    def quotas(pool, environ):
        assert [(row.name, row.env_name) for row in pool] == [("a", "AH_CX_TOKEN_a"), ("b", "AH_CX_TOKEN_b")]
        return {
            name: CodexQuota(100, "pro", balancer.QuotaWindow(used=10), balancer.QuotaWindow(used=20))
            for name in ("a", "b")
        }

    monkeypatch.setattr(capacity.codex_router, "quotas", quotas)
    seen = capacity.accounts({}, 100)
    assert [(row.name, row.sessions, row.week_left) for row in seen] == [("a", 1, 80), ("b", 2, 80)]


def test_runtime_honors_reserved_harness_seats(tmp_path):
    from scripts.swarm.runtime import HerdrRuntime

    runtime = HerdrRuntime(home=tmp_path)
    runtime._quota_accounts = [account(), account("cx", harness="codex")]
    runtime._quota_cap, runtime._quota_floor, runtime._quota_share = 3, 5, 30
    runtime._quota_allocations = {"eng": {"claude": 0, "codex": 1}, "ci": {"claude": 1, "codex": 0}}
    assert runtime._quota_choice("claude", "priority", False, "eng") == (
        "codex",
        "fallthrough: claude has no placeable quota seats",
    )
    assert runtime._quota_choice("claude", "requested", True, "ci") == ("claude", "requested")


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
    assert seen[0].state == "BLOCKED"
    assert capacity.free_seats(seen[0], 3, 5) == 0


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
    decision = capacity.calculate(config, observed, [], 3, 5, {"eng": 1, "ci": 1, "plan": 0}, requirements)
    runtime._quota_accounts = observed
    runtime._quota_cap, runtime._quota_floor, runtime._quota_share = 3, 5, 30
    runtime._quota_allocations = decision["allocation"]
    assert runtime._quota_choice("claude", "required", True, "eng") == ("claude", "required")
    assert runtime._quota_choice("claude", "priority", False, "ci")[0] == "codex"
    assert decision["effective"] == {"eng": 1, "ci": 1, "plan": 0}


def test_unobserved_live_account_stays_visible_and_unplaceable(monkeypatch):
    monkeypatch.setattr(balancer, "cached_observations", lambda **kw: [])
    monkeypatch.setattr(capacity.account_sessions, "sessions_by_account", lambda: {"unknown": 2})
    monkeypatch.setattr(capacity.codex_router, "routing_pool", lambda env: [])
    assert capacity.accounts({}, 100) == [capacity.Account("claude", "unknown", "UNKNOWN", 2, None, None)]


@pytest.mark.parametrize(("five", "week"), [(None, 50), (50, None)])
def test_partial_observations_never_place_a_session(five, week):
    assert capacity.free_seats(capacity.Account("claude", "a", "NORMAL", 0, five, week), 3, 5) == 0


def test_odd_reduced_cap_and_exact_codex_week_floor():
    assert capacity.free_seats(account(state="REDUCE", left=30), 3, 5) == 1
    assert capacity.free_seats(account(harness="codex", state="NORMAL", left=5), 3, 5) == 3


def test_zero_share_keeps_automatic_lanes_on_claude():
    config = SwarmConfig("sw", "/repo", max_eng=2, max_ci=0, max_plan=0, codex_share=0)
    decision = capacity.calculate(config, [account(state="DRAIN", left=4), account("cx", harness="codex")], [], 3, 5)
    assert decision["effective"] == {"eng": 0, "ci": 0, "plan": 0}
    assert decision["reason"] == "accounts are drain; Claude has 0 free seats and Codex has 3 free seats"


def test_finished_agents_do_not_reserve_capacity_and_reason_lists_all_restrictions():
    config = SwarmConfig("sw", "/repo", max_eng=2, max_ci=1, max_plan=0)
    dead = [AgentRecord("finished", "eng", "e", state="finished")]
    seen = [account("z", state="DRAIN_SOON", left=15), account("a", state="REDUCE", left=30), account("b")]
    decision = capacity.calculate(config, seen, dead, 3, 5)
    assert decision == {
        "configured": {"eng": 2, "ci": 1, "plan": 0},
        "effective": {"eng": 2, "ci": 1, "plan": 0},
        "placeable": {"claude": 4, "codex": 0},
        "reason": "accounts are drain soon, reduce; Claude has 4 free seats and Codex has 0 free seats",
        "placements": {
            "eng": [{"index": 0, "harness": "claude"}, {"index": 1, "harness": "claude"}],
            "ci": [{"index": 0, "harness": "claude"}],
            "plan": [],
        },
        "accounts": [row.__dict__ for row in seen],
        "allocation": {
            "eng": {"claude": 2, "codex": 0},
            "ci": {"claude": 1, "codex": 0},
            "plan": {"claude": 0, "codex": 0},
        },
    }


def test_runtime_capacity_uses_the_current_environment_and_saves_the_allocation(tmp_path, monkeypatch):
    from scripts.swarm import runtime as module

    seen = [account(), account("cx", harness="codex")]
    monkeypatch.setenv("AGENTIHOOKS_MAX_SESSIONS_PER_ACCOUNT", "4")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_CODEX_SHARE", "0")

    def observations(env, now):
        assert env["AGENTIHOOKS_MAX_SESSIONS_PER_ACCOUNT"] == "4"
        assert now == 123
        return seen

    monkeypatch.setattr(capacity, "accounts", observations)
    rt = module.HerdrRuntime(home=tmp_path, choose=lambda *args: ("claude", module.agent_choice.ALL_FULL))
    config = SwarmConfig("sw", "/repo", max_eng=2, max_ci=1, max_plan=0, codex_min_week_left=5)
    decision = rt.quota_capacity(
        config, [], 123, {"eng": 1, "ci": 0, "plan": 0}, {"eng": [("claude",)], "ci": [], "plan": []}
    )
    assert decision["effective"] == {"eng": 1, "ci": 0, "plan": 0}
    assert rt._quota_accounts == seen
    assert (rt._quota_cap, rt._quota_share, rt._quota_floor) == (4, 0, 5)
    assert rt._quota_allocations == {
        "eng": {"claude": 1, "codex": 0},
        "ci": {"claude": 0, "codex": 0},
        "plan": {"claude": 0, "codex": 0},
    }
    assert rt.has_capacity(config)
    rt._quota_accounts = [account(state="DRAIN", left=4)]
    assert not rt.has_capacity(config)


def test_status_command_prints_the_capacity_reason(monkeypatch, capsys):
    from types import SimpleNamespace

    from scripts.swarm import cli

    store = _store()
    store.create(SwarmConfig("sw", "/repo", max_eng=2, max_ci=1))
    ledger = FakeLedger([])
    monkeypatch.setattr(cli, "LedgerClient", lambda: ledger)
    store.redis.set(
        store.key("sw", "quota-capacity"), '{"effective":{"eng":0,"ci":0,"plan":0},"reason":"accounts are drain"}'
    )
    cli.cmd_status(store, SimpleNamespace(slug="sw", json=False))
    assert "quota capacity eng 0 ci 0 plan 0 because accounts are drain\n" in capsys.readouterr().out


def test_codex_quota_reset_and_signed_out_accounts(monkeypatch):
    from scripts.codex_quota import CodexQuota

    environ = {"marker": "test"}
    monkeypatch.setattr(balancer, "cached_observations", lambda **kw: [])
    monkeypatch.setattr(capacity.account_sessions, "sessions_by_account", lambda: {})
    pool = [
        capacity.codex_router.CodexAccount("a", "AH_CX_TOKEN_a"),
        capacity.codex_router.CodexAccount("b", "AH_CX_TOKEN_b", signed_in=False),
    ]

    def routing(env):
        assert env == environ
        return pool

    def quotas(accounts, env):
        assert env == environ and accounts == [pool[0]]
        return {
            "a": CodexQuota(
                100, "pro", balancer.QuotaWindow(used=95, resets_at=200), balancer.QuotaWindow(used=80, resets_at=200)
            )
        }

    monkeypatch.setattr(capacity.codex_router, "routing_pool", routing)
    monkeypatch.setattr(capacity.codex_router, "quotas", quotas)
    assert capacity.accounts(environ, 199) == [capacity.Account("codex", "a", "DRAIN", 0, 5, 20)]
    assert capacity.accounts(environ, 200) == [capacity.Account("codex", "a", "NORMAL", 0, 100, 100)]


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
        capacity, "accounts", lambda env, now: [account(state="DRAIN", left=3), account("cx", harness="codex")]
    )
    decision = runtime.quota_capacity(config, [], 100, {"eng": 2, "ci": 0, "plan": 0}, requirements)
    assert decision["effective"] == {"eng": 1, "ci": 0, "plan": 0}
    assert decision["placements"] == {"eng": [{"index": 1, "harness": "codex"}], "ci": [], "plan": []}
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
        capacity, "accounts", lambda env, now: [account(sessions=2), account("cx", sessions=2, harness="codex")]
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
    runtime._quota_cap, runtime._quota_floor, runtime._quota_share = 3, 5, 30
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


def test_new_launch_uses_the_best_tightest_quota_and_consumes_its_slot(tmp_path, monkeypatch):
    observed = [account("a", left=80), capacity.Account("claude", "b", "NORMAL", 0, 90, 40)]
    runtime, config, seen = _runtime_probe(tmp_path, monkeypatch, observed)
    runtime._quota_allocations = {"plan": {"claude": 2, "codex": 0}}
    runtime.spawn(config, "plan", "planner@a1b2c3-0001", {"id": "p", "title": "Plan"})
    assert seen == [("p", "claude", "a")]
    assert runtime._quota_accounts == [account("a", sessions=1, left=80), observed[1]]
    assert runtime._quota_allocations == {"plan": {"claude": 1, "codex": 0}}


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

    runtime, config, seen = _runtime_probe(
        tmp_path, monkeypatch, [account(state="DRAIN", left=4), account("cx", harness="codex")]
    )
    task = {"id": "p", "title": "Plan"}
    if fixed == "profile":
        task["profile"] = "frontend"
    elif fixed == "lane":
        config = replace(config, lanes={"plan": {"agent": "claude"}})
    else:
        task["handoff_envelope"] = {
            "launch": {"profile": "planner", "harness": "claude", "model": "fable", "effort": "high"}
        }
    with pytest.raises(SpawnError, match="^no claude account has placeable quota seats$"):
        runtime.spawn(config, "plan", "planner@a1b2c3-0001", task)
    assert seen == []


def test_verified_account_seats_override_an_old_harness_cap_choice(tmp_path, monkeypatch):
    from scripts import agent_choice

    runtime, config, seen = _runtime_probe(tmp_path, monkeypatch, [account()], reason=agent_choice.ALL_FULL)
    runtime.spawn(config, "plan", "planner@a1b2c3-0001", {"id": "p", "title": "Plan"})
    assert seen == [("p", "claude", "a")]


def test_zero_share_refuses_automatic_codex_but_keeps_automatic_claude(tmp_path):
    from scripts.swarm.runtime import HerdrRuntime
    from scripts.swarm.tick import SpawnError

    runtime = HerdrRuntime(home=tmp_path)
    runtime._quota_cap, runtime._quota_floor, runtime._quota_share = 3, 5, 0
    runtime._quota_accounts = [account(state="DRAIN", left=4), account("cx", harness="codex")]
    with pytest.raises(SpawnError, match="^no claude account has placeable quota seats$"):
        runtime._quota_choice("claude", "priority", False, "eng")
    runtime._quota_accounts = [account(), account("cx", harness="codex", state="DRAIN", left=4)]
    assert runtime._quota_choice("codex", "priority", False, "eng") == (
        "claude",
        "fallthrough: codex has no placeable quota seats",
    )


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
        return capacity.calculate(cfg, [account()], agents, 3, 5, demand)

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
    runtime.quota_capacity = lambda cfg, agents, now, demand, requirements: capacity.calculate(
        cfg, [], agents, 3, 5, demand
    )
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
        cfg, [account()], agents, 3, 5, demand
    )
    ledger = FakeLedger([{"id": "e"}])
    ledger.capacity_comment = ledger_client.LedgerClient().capacity_comment
    capacity.apply("sw", config, store, ledger, runtime, 1000)
    (op,) = sent
    ledger_core.check_op(op)


def test_a_refused_ledger_write_is_skipped_and_spawning_still_runs(capsys):
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
        cfg, [account()], agents, 3, 5, demand
    )
    actions = tick("sw", store, ledger, runtime, 1000)
    assert [task for _, _, task in runtime.spawned] == ["e"]
    assert "skipped scripts.swarm.capacity.apply: the ledger refused its write" in actions
    assert capsys.readouterr().err == (
        "scripts.swarm.capacity.apply skipped, the ledger refused its write: "
        "ledger sw: server refused: 400 by is allowed only on agent chat and comment entries\n"
    )


def test_inherited_zero_codex_share_is_respected_when_planning_ready_tasks(tmp_path, monkeypatch):
    from scripts.swarm.runtime import HerdrRuntime

    monkeypatch.setenv("AGENTIHOOKS_SWARM_CODEX_SHARE", "0")
    monkeypatch.setattr("scripts.swarm.runtime.plugins.claude_only", lambda _: False)
    runtime = HerdrRuntime(home=tmp_path)
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0)
    requirements = runtime.quota_requirements(config, {"eng": [{"id": "e"}], "ci": [], "plan": []})
    assert requirements == {"eng": [("claude",)], "ci": [], "plan": []}


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
    decision = capacity.calculate(config, [account(sessions=2)], [], 3, 5)
    assert decision["effective"] == {"eng": 1, "ci": 0, "plan": 0}


def test_balanced_free_harnesses_keep_the_declared_priority():
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0)
    decision = capacity.calculate(config, [account(sessions=2), account("cx", sessions=2, harness="codex")], [], 3, 5)
    assert decision["placements"] == {"eng": [{"index": 0, "harness": "claude"}], "ci": [], "plan": []}


def test_reservations_ignore_fixed_work_beyond_the_configured_lane_cap():
    config = SwarmConfig("sw", "/repo", max_eng=2, max_ci=0, max_plan=0)
    live = [AgentRecord("live", "eng", "active")]
    requirements = {"eng": [("claude", "codex"), ("claude",)], "ci": [], "plan": []}
    seen = [account(sessions=2), account("cx", sessions=2, harness="codex")]
    decision = capacity.calculate(config, seen, live, 3, 5, {"eng": 2, "ci": 0, "plan": 0}, requirements)
    assert decision["placements"] == {"eng": [{"index": 0, "harness": "claude"}], "ci": [], "plan": []}


def test_runtime_keeps_one_environment_snapshot_for_account_limits(tmp_path, monkeypatch):
    from scripts.swarm.runtime import HerdrRuntime

    monkeypatch.setenv("AGENTIHOOKS_MAX_SESSIONS_PER_ACCOUNT", "4")
    config = SwarmConfig("sw", "/repo", max_eng=4, max_ci=1, max_plan=0, codex_share=0)

    def observations(env, now):
        assert env["AGENTIHOOKS_MAX_SESSIONS_PER_ACCOUNT"] == "4"
        monkeypatch.setenv("AGENTIHOOKS_MAX_SESSIONS_PER_ACCOUNT", "1")
        return [account()]

    monkeypatch.setattr(capacity, "accounts", observations)
    runtime = HerdrRuntime(home=tmp_path)
    decision = runtime.quota_capacity(config, [], 100, {"eng": 1, "ci": 0, "plan": 0})
    assert runtime._quota_cap == 4
    assert decision["effective"] == {"eng": 1, "ci": 0, "plan": 0}
    runtime._quota_accounts = [account("cx", harness="codex", state="DRAIN", left=4)]
    assert runtime.has_capacity(config) is False


def test_runtime_capacity_without_requirements_honors_the_inherited_share(tmp_path, monkeypatch):
    from scripts.swarm.runtime import HerdrRuntime

    monkeypatch.setenv("AGENTIHOOKS_SWARM_CODEX_SHARE", "0")
    monkeypatch.setattr(capacity, "accounts", lambda env, now: [account("cx", harness="codex")])
    runtime = HerdrRuntime(home=tmp_path)
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0)
    assert runtime.quota_capacity(config, [], 100)["effective"] == {"eng": 0, "ci": 0, "plan": 0}


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
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0, codex_share=30, lanes={"eng": chosen})
    assert runtime.quota_requirements(config, {"eng": [task], "ci": [], "plan": []})["eng"] == [expected]


def test_capacity_apply_preserves_saved_options_and_controller_evidence(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from scripts.swarm import ledger_client
    from scripts.swarm import runtime as module

    store = _store()
    config = SwarmConfig("sw", "/repo", max_eng=3, max_ci=0, max_plan=0, codex_share=30)
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

    def observations(env, now):
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
    assert calls[0]["text"] == result[0] and calls[0]["thread"] == "tasks/fixed/comments"


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


def test_live_account_caps_feed_quota_limits(monkeypatch):
    from scripts import session_caps
    from scripts.codex_quota import CodexQuota

    windows = balancer.QuotaWindow(used=10)
    row = balancer.ProbeResult("a", "allowed", "NORMAL", 90, windows, windows)
    monkeypatch.setattr(balancer, "cached_observations", lambda **kw: [(100, row)])
    monkeypatch.setattr(capacity.account_sessions, "sessions_by_account", lambda: {"a": 1, "unknown": 0})
    monkeypatch.setattr(capacity.account_sessions, "codex_sessions_by_account", lambda: {"cx": 1})
    monkeypatch.setattr(
        capacity.codex_router, "routing_pool", lambda env: [capacity.codex_router.CodexAccount("cx", "AH_CX_TOKEN_cx")]
    )
    monkeypatch.setattr(
        capacity.codex_router, "quotas", lambda pool, env: {"cx": CodexQuota(100, "pro", windows, windows)}
    )
    monkeypatch.setattr(
        session_caps, "stored", lambda harness: {"a": 2, "unknown": 0} if harness == "claude" else {"cx": 5}
    )
    observed = capacity.accounts({}, 100)
    assert [(row.harness, row.name, row.cap) for row in observed] == [
        ("claude", "a", 2),
        ("claude", "unknown", 0),
        ("codex", "cx", 5),
    ]
    assert [capacity.free_seats(row, 7, 5) for row in observed] == [1, 0, 4]


def test_closed_or_reduced_account_caps_never_use_the_global_default():
    closed = capacity.Account("claude", "a", "NORMAL", 0, 90, 90, cap=0)
    reduced = capacity.Account("codex", "cx", "REDUCE", 1, 30, 30, cap=5)
    assert capacity.free_seats(closed, 7, 5) == 0
    assert capacity.free_seats(reduced, 7, 5) == 1


def test_lane_harness_pin_wins_over_a_saved_different_harness(tmp_path, monkeypatch):
    from scripts.swarm import runtime as module

    monkeypatch.setattr(module.plugins, "claude_only", lambda profile: False)
    runtime = module.HerdrRuntime(home=tmp_path)
    config = SwarmConfig(
        "sw", "/repo", max_eng=1, max_ci=0, max_plan=0, lanes={"eng": {"agent": "codex"}}, codex_share=30
    )
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

    runtime, config, seen = _runtime_probe(
        tmp_path, monkeypatch, [account(), account("cx", harness="codex", state="DRAIN", left=4)]
    )
    monkeypatch.setattr(module.affinity, "desired", lambda cfg: "codex")
    if planned:
        runtime._quota_tasks = {"p": "claude"}
    with pytest.raises(SpawnError, match="^no codex account has placeable quota seats$"):
        runtime.spawn(config, "master", "master@a1b2c3-0001", {"id": "p", "title": "Master", "profile": "master"})
    assert seen == []


def test_todays_accounts_get_seats_by_spendable_rate(monkeypatch):
    now, hour = 1_800_000_000, 3600

    def observed(name, five_used, week_left, week_hours):
        five = balancer.QuotaWindow(used=five_used, resets_at=now + 2 * hour)
        week = balancer.QuotaWindow(used=100 - week_left, resets_at=now + int(week_hours * hour))
        return now, balancer.ProbeResult(name, "allowed_warning", "DRAIN", week_left, five, week)

    today = [
        observed("ncsmgma", 0, 4, 4.9),
        observed("tccgma", 20, 19, 96),
        observed("nctcc", 0, 10, 96),
        observed("ncgma", 0, 100, 168),
    ]
    monkeypatch.setattr(balancer, "cached_observations", lambda **kw: today)
    monkeypatch.setattr(capacity.account_sessions, "sessions_by_account", lambda: {"ncgma": 1})
    monkeypatch.setattr(capacity.codex_router, "routing_pool", lambda env: [])
    rows = {row.name: row for row in capacity.accounts({}, now)}
    assert {name: row.state for name, row in rows.items()} == {
        "ncsmgma": "NORMAL",
        "tccgma": "REDUCE",
        "nctcc": "DRAIN",
        "ncgma": "NORMAL",
    }
    seats = {name: capacity.free_seats(row, 4, 5) for name, row in rows.items()}
    assert seats == {"ncsmgma": 4, "tccgma": 2, "nctcc": 0, "ncgma": 3}


def test_drain_soon_is_guarded_by_the_five_hour_window_only():
    assert capacity.free_seats(capacity.Account("claude", "a", "DRAIN_SOON", 0, 20, 4), 3, 5) == 3
    assert capacity.free_seats(capacity.Account("claude", "a", "DRAIN_SOON", 0, 19, 90), 3, 5) == 0
