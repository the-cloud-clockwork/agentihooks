import fakeredis
import pytest

from scripts import claude_quota_balancer as balancer
from scripts.swarm import capacity
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeLedger, FakeRuntime

pytestmark = pytest.mark.xdist_group("fakeredis")


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
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    config = SwarmConfig("sw", "/repo", max_eng=2, max_ci=1, max_plan=0, state="running")
    store.create(config)
    ledger = FakeLedger([{"id": "e"}, {"id": "c", "lane": "ci"}])
    ledger.comments = []
    ledger.comment = lambda slug, item, text, by: ledger.comments.append((slug, item, text, by))
    runtime = FakeRuntime()
    monkeypatch.setattr(capacity, "accounts", lambda env, now: [account()])
    runtime.quota_capacity = lambda cfg, agents, now: capacity.calculate(cfg, capacity.accounts({}, now), agents, 3, 5)
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

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
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
        lambda credentials, **kwargs: seen.append(([r.account for r in credentials], kwargs)),
    )
    monkeypatch.setattr(balancer, "cached_observations", lambda **kwargs: [])
    monkeypatch.setattr(capacity.account_sessions, "sessions_by_account", lambda: {})
    monkeypatch.setattr(capacity.codex_router, "routing_pool", lambda _: [])
    capacity.accounts(environ, 123)
    assert seen == [(["a", "b"], {"environ": environ, "now": 123})]
