import json
from dataclasses import replace

import fakeredis
import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import capacity, quota_handoff, runtime
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeLedger, FakeRuntime

pytestmark = pytest.mark.xdist_group("fakeredis")


def account(name="spent", harness="claude", five=50, week=90, state="NORMAL", sessions=1):
    return capacity.Account(harness, name, state, sessions, 100 - five, 100 - week)


@pytest.mark.parametrize(
    "five,week,expected",
    [(94.99, 89.99, ""), (95, 89, "five hour"), (94, 90, "week"), (100, 100, "week")],
)
def test_warning_thresholds(five, week, expected):
    assert quota_handoff.trigger(account(five=five, week=week), quota_handoff.Thresholds()) == expected


def test_warning_thresholds_are_configurable():
    thresholds = quota_handoff.Thresholds.from_env(
        {"AGENTIHOOKS_HANDOFF_WARN_WEEK_PCT": "80", "AGENTIHOOKS_HANDOFF_WARN_5H_PCT": "85"}
    )
    assert thresholds == quota_handoff.Thresholds(80, 85)
    assert quota_handoff.trigger(account(five=84, week=79), thresholds) == ""
    assert quota_handoff.trigger(account(five=85, week=79), thresholds) == "five hour"
    assert quota_handoff.trigger(account(five=84, week=80), thresholds) == "week"


@pytest.mark.parametrize(
    "environ",
    [
        {"AGENTIHOOKS_HANDOFF_WARN_WEEK_PCT": "98"},
        {"AGENTIHOOKS_HANDOFF_WARN_5H_PCT": "99"},
        {"AGENTIHOOKS_HANDOFF_WARN_WEEK_PCT": "0"},
        {"AGENTIHOOKS_HANDOFF_WARN_5H_PCT": "-1"},
        {"AGENTIHOOKS_HANDOFF_WEEK_PCT": "89"},
        {"AGENTIHOOKS_HANDOFF_5H_PCT": "94"},
    ],
)
def test_warning_must_precede_hard_handoff(environ):
    with pytest.raises(
        ValueError, match="^quota warning thresholds must be positive and below the hard handoff thresholds$"
    ):
        quota_handoff.Thresholds.from_env(environ)


def test_a_new_agent_gets_its_own_warning_after_a_reset():
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.put_agent("sw", AgentRecord("first", "eng", "e", harness="claude", account="spent"))
    key = store.key("sw", "quota-capacity")
    store.redis.set(key, json.dumps({"accounts": [account().__dict__]}))
    assert quota_handoff.warn("sw", store, {}) == ["early quota handoff warning sent to first"]
    store.redis.set(key, json.dumps({"accounts": [account(week=0).__dict__]}))
    assert quota_handoff.warn("sw", store, {}) == []
    store.redis.set(key, json.dumps({"accounts": [account().__dict__]}))
    assert quota_handoff.warn("sw", store, {}) == []
    store.put_agent("sw", AgentRecord("second", "eng", "f", harness="claude", account="spent"))
    assert quota_handoff.warn("sw", store, {}) == ["early quota handoff warning sent to second"]
    assert len(InboxStore(store.redis).pending_items("first")) == 1
    assert len(InboxStore(store.redis).pending_items("second")) == 1


@pytest.mark.parametrize("field", ["five_left", "week_left"])
def test_unknown_quota_does_not_warn(field):
    assert quota_handoff.trigger(replace(account(), **{field: None}), quota_handoff.Thresholds()) == ""


def test_tick_warns_once_per_agent_without_retiring_it(monkeypatch):
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0, state="running", code="a1b2c3")
    store.create(config)
    agent = AgentRecord("engineer@a1b2c3-0001", "eng", "e", harness="claude", account="spent", state="working")
    store.put_agent("sw", agent)
    store.claim("sw", "e", agent.name, 60_000)
    ledger = FakeLedger([{"id": "e", "state": "claimed", "claimed_by": agent.name}])
    ledger.comment = lambda *args, **kwargs: None
    rt = FakeRuntime()
    rt.live.add(agent.name)
    rt.quota_capacity = lambda cfg, agents, now, demand: capacity.calculate(cfg, [account()], agents, 3, 5, demand)
    tick("sw", store, ledger, rt, 1000)
    tick("sw", store, ledger, rt, 2000)
    messages = InboxStore(store.redis).pending_items(agent.name)
    assert len(messages) == 1
    assert messages[0].text == (
        "QUOTA HANDOFF WARNING: claude account spent has used 90% of its week window. "
        "Finish your current step and write your Handoff v2 with the handoff skill while quota remains. "
        "Submit it with agentihooks swarm sw handoff DOC --reason quota, then stop. "
        "The tick keeps your seat and task and routes the successor to an account with room. "
        "This warning does not block tools or terminate your running agent."
    )
    assert "Finish your current step" in messages[0].text
    assert "Handoff v2" in messages[0].text
    assert "--reason quota" in messages[0].text
    assert not rt.killed
    assert not rt.nudged
    assert store.agents("sw")[0].name == agent.name


def test_warning_matches_account_and_harness_and_skips_finished_agents():
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    for name, harness, state in [("healthy", "claude", "working"), ("finished", "codex", "finished")]:
        store.put_agent("sw", AgentRecord(name, "eng", "e", harness=harness, account="spent", state=state))
    store.redis.set(store.key("sw", "quota-capacity"), json.dumps({"accounts": [account(harness="codex").__dict__]}))
    assert quota_handoff.warn("sw", store, {}) == []
    assert not InboxStore(store.redis).pending_items("healthy")
    assert not InboxStore(store.redis).pending_items("finished")


@pytest.mark.parametrize("state", ["DRAIN", "DRAIN_SOON", "REDUCE"])
def test_successor_uses_most_routing_left_and_skips_unplaceable_accounts(state):
    rows = [
        account("old", week=91),
        account("blocked", five=0, week=0, state=state, sessions=3),
        account("middle", five=30, week=40),
        account("best", five=10, week=20),
        account("cx", "codex", five=0, week=0),
    ]
    assert quota_handoff.successor(rows, 3, 5, True, quota_handoff.Thresholds()).name == "best"


def test_successor_falls_back_to_codex_and_respects_profile_and_floor():
    rows = [account("cc", state="DRAIN"), account("cx", "codex", five=10, week=20)]
    assert quota_handoff.successor(rows, 3, 5, True, quota_handoff.Thresholds()) == rows[1]
    assert quota_handoff.successor(rows, 3, 5, False, quota_handoff.Thresholds()) is None
    assert quota_handoff.successor(rows, 3, 81, True, quota_handoff.Thresholds()) is None


@pytest.mark.parametrize("target", ["claude", "codex"])
@pytest.mark.parametrize("lane", ["eng", "ci", "plan", "master"])
def test_quota_transfer_routes_to_best_account_and_preserves_profile(tmp_path, monkeypatch, target, lane):
    rt = runtime.HerdrRuntime(home=tmp_path, choose=lambda *_: ("claude", "priority"))
    rt._quota_accounts = [account("old", state="DRAIN"), account("best", target, five=10, week=20)]
    rt._quota_cap, rt._quota_floor, rt._quota_share = 3, 5, 0
    seen = []
    monkeypatch.setattr(runtime.plugins, "claude_only", lambda _: False)
    monkeypatch.setattr(
        rt,
        "_launch",
        lambda cfg, lane, task, name, argv, **kw: seen.append(argv) or runtime.Placed("pane", target, "best"),
    )
    config = SwarmConfig("sw", str(tmp_path), max_eng=1, max_ci=0, code="a1b2c3", lanes={lane: {"model": "opus"}})
    task = {
        "id": "e",
        "title": "Continue task",
        "handoff": "Saved Handoff v2",
        "handoff_envelope": {
            "reason": "quota",
            "launch": {"profile": "engineer", "harness": "claude", "model": "opus", "effort": "high", "account": "old"},
        },
    }
    placed = rt.spawn(config, lane, "engineer@a1b2c3-0002", task)
    assert placed.harness == target
    assert seen[0][seen[0].index("--route") + 1] == "best"
    assert seen[0][seen[0].index("--profile") + 1] == "engineer"
    assert seen[0][seen[0].index("--agent") + 1] == target
    if target == "codex":
        assert "opus" not in seen[0]


def test_quota_successor_uses_its_lane_reservation(tmp_path, monkeypatch):
    rt = runtime.HerdrRuntime(home=tmp_path, choose=lambda *_: ("claude", "priority"))
    rt._quota_accounts = [account("cc", five=0, week=0), account("cx", "codex", five=10, week=20)]
    rt._quota_cap, rt._quota_floor, rt._quota_share = 3, 5, 30
    rt._quota_allocations = {"eng": {"claude": 0, "codex": 1}, "ci": {"claude": 1, "codex": 0}}
    monkeypatch.setattr(runtime.plugins, "claude_only", lambda _: False)
    monkeypatch.setattr(rt, "_launch", lambda *args, **kw: runtime.Placed("pane", "codex", "cx"))
    config = SwarmConfig("sw", str(tmp_path), max_eng=1, max_ci=1, code="a1b2c3")
    task = {
        "id": "e",
        "title": "Continue task",
        "handoff": "Saved Handoff v2",
        "handoff_envelope": {
            "reason": "quota",
            "launch": {"profile": "engineer", "harness": "claude", "model": "opus", "effort": "high", "account": "old"},
        },
    }
    placed = rt.spawn(config, "eng", "engineer@a1b2c3-0002", task)
    assert placed.harness == "codex"
    assert rt._quota_allocations == {"eng": {"claude": 0, "codex": 0}, "ci": {"claude": 1, "codex": 0}}
