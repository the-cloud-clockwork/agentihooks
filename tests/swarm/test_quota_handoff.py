import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import capacity, quota_handoff, quota_notice, runtime
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm.tick import tick
from tests.swarm.profile_fixture import validated
from tests.swarm.test_tick import FakeLedger, FakeRuntime, store, tasks, workers  # noqa: F401

pytestmark = pytest.mark.xdist_group("fakeredis")

LAUNCH = {"profile": "engineer", "harness": "claude", "model": "opus", "effort": "high", "account": "old"}


class QuotaRuntime(FakeRuntime):
    def __init__(self, home, pool, monkeypatch):
        super().__init__()
        self.argv, self.chosen = [], []
        monkeypatch.setattr(capacity, "accounts", lambda environ, now, refresh=True: list(pool))
        self.herdr = HerdrRuntime(home=home, run=self._run, choose=self._choose, herdr=self._herdr)

    def _choose(self, requested, environ):
        self.chosen.append(requested)
        return requested, "requested"

    def _herdr(self, args):
        raise AssertionError(f"unexpected herdr call {args}")

    def _run(self, argv, **kwargs):
        self.argv.append(argv)
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    def has_capacity(self, config):
        return self.herdr.has_capacity(config)

    def quota_capacity(self, *args, **kwargs):
        return self.herdr.quota_capacity(*args, **kwargs)

    def quota_requirements(self, config, ready):
        return self.herdr.quota_requirements(config, ready)

    def spawn(self, config, lane, name, task):
        if not task.get("handoff"):
            return super().spawn(config, lane, name, task)
        placed = self.herdr.spawn(config, lane, name, task)
        self.live.add(name)
        self.spawned.append((lane, name, task["id"]))
        self.tasks.append(dict(task))
        return placed


def _ledger():
    ledger = tasks(("t1", "eng"), ("t2", "eng"))
    ledger.comments = []
    ledger.comment = lambda slug, task, text, by: ledger.comments.append((task, text))
    return ledger


def _quota_handoff(store, ledger, runtime):  # noqa: F811
    for task in ledger.rows.values():
        task["title"] = "keep the seat"
    tick("sw", store, ledger, runtime, 1)
    done, first = sorted(workers(store), key=lambda agent: agent.seat)
    assert (done.seat, first.seat) == ("eng-1@sw", "eng-2@sw")
    ledger.rows[done.task].update(state="done", done=True)
    envelope = {"reason": "quota", "agent": first.name, "task": first.task, "launch": LAUNCH}
    store.put_handoff("sw", first.task, "handoff document", seat=first.seat, envelope=envelope)
    for agent in (done, first):
        store.put_agent("sw", replace(agent, state="finished"))
    return done, first, envelope


def _capacity(eng, claude, codex):
    return (
        f"quota capacity eng {eng} ci 0 plan 0 because accounts have quota; "
        f"Claude has {claude} free seats and Codex has {codex} free seats"
    )


def _held(task):
    return (
        f"; quota handoff {task} waits: no claude or codex account can take the quota handoff "
        "from claude account old: claude old is the account handing off"
    )


def _option(argv, flag):
    return argv[argv.index(flag) + 1]


OLD = capacity.Account("claude", "old", "OPEN", 0, 5, 90, 2)
FRESH = capacity.Account("claude", "fresh", "OPEN", 1, 90, 90, 6)


def test_a_quota_handoff_keeps_the_seat_and_launch_settings_on_another_account(store, tmp_path, monkeypatch):  # noqa: F811
    pool = [OLD, FRESH, capacity.Account("codex", "roomy", "OPEN", 0, 100, 100, 6)]
    ledger, runtime = _ledger(), QuotaRuntime(tmp_path, pool, monkeypatch)
    done, first, envelope = _quota_handoff(store, ledger, runtime)
    actions = tick("sw", store, ledger, runtime, 2)
    (argv,) = runtime.argv
    successor = next(a for a in workers(store) if a.name != first.name)
    assert actions == [
        f"retired {done.name}",
        f"retired {first.name}",
        _capacity(1, 7, 6),
        f"spawned {successor.name} for {first.task}",
    ]
    assert successor.seat == first.seat
    assert runtime.chosen == []
    assert runtime.tasks[-1]["handoff_envelope"] == envelope
    assert _option(argv, "--route") == "fresh"
    assert [_option(argv, flag) for flag in ("--profile", "--agent", "--model", "--effort")] == [
        "engineer",
        "claude",
        "opus",
        "high",
    ]
    assert store.handoff("sw", first.task) == ""


def _waits_with_its_handoff(store, ledger, runtime, first, envelope):  # noqa: F811
    assert runtime.argv == []
    assert ledger.rows[first.task]["state"] == "open"
    assert (store.handoff("sw", first.task), store.handoff_seat("sw", first.task)) == ("handoff document", first.seat)
    assert store.handoff_envelope("sw", first.task) == envelope


def test_a_quota_handoff_waits_with_its_handoff_when_no_other_account_qualifies(store, tmp_path, monkeypatch):  # noqa: F811
    pool = [OLD]
    ledger, runtime = _ledger(), QuotaRuntime(tmp_path, pool, monkeypatch)
    done, first, envelope = _quota_handoff(store, ledger, runtime)
    actions = tick("sw", store, ledger, runtime, 2)
    assert actions == [f"retired {done.name}", f"retired {first.name}", _capacity(0, 2, 0) + _held(first.task)]
    _waits_with_its_handoff(store, ledger, runtime, first, envelope)
    pool.append(FRESH)
    tick("sw", store, ledger, runtime, 3)
    (argv,) = runtime.argv
    (successor,) = (a for a in workers(store) if a.task == first.task and a.name != first.name)
    assert _option(argv, "--route") == "fresh"
    assert successor.seat == first.seat


def test_a_quota_handoff_waits_with_its_handoff_while_every_account_is_full(store, tmp_path, monkeypatch):  # noqa: F811
    pool = [OLD]
    ledger, runtime = _ledger(), QuotaRuntime(tmp_path, pool, monkeypatch)
    done, first, envelope = _quota_handoff(store, ledger, runtime)
    pool[0] = replace(OLD, sessions=2)
    actions = tick("sw", store, ledger, runtime, 2)
    assert actions == [f"retired {done.name}", f"retired {first.name}", _capacity(0, 0, 0) + _held(first.task)]
    _waits_with_its_handoff(store, ledger, runtime, first, envelope)


@pytest.mark.parametrize("claude_only", [False, True])
@pytest.mark.parametrize("codex_account", ["roomy", "old"])
def test_quota_successor_falls_back_during_tick_without_losing_its_seat(
    store,  # noqa: F811
    tmp_path,
    monkeypatch,
    claude_only,
    codex_account,
):
    pool = [OLD, capacity.Account("codex", codex_account, "OPEN", 0, 90, 90, 6)]
    ledger, rt = _ledger(), QuotaRuntime(tmp_path, pool, monkeypatch)
    _, first, envelope = _quota_handoff(store, ledger, rt)
    pool[0] = replace(OLD, state="DRAIN", cap=0)
    monkeypatch.setattr(runtime.plugins, "claude_only", lambda _: claude_only)
    tick("sw", store, ledger, rt, 2)
    if claude_only:
        _waits_with_its_handoff(store, ledger, rt, first, envelope)
    else:
        (argv,) = rt.argv
        successor = next(a for a in workers(store) if a.task == first.task and a.name != first.name)
        assert successor.seat == first.seat
        assert [_option(argv, flag) for flag in ("--agent", "--route", "--profile")] == [
            "codex",
            codex_account,
            "engineer",
        ]
        assert "opus" not in argv
        assert store.handoff("sw", first.task) == ""


def account(name="spent", harness="claude", five=50, week=90, state="OPEN", sessions=1):
    cap = 0 if state in ("DRAIN", "DRAIN_SOON") else 1 if state == "REDUCE" else 3
    return capacity.Account(harness, name, state, sessions, 100 - five, 100 - week, cap)


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
    import fakeredis

    storage = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    storage.put_agent("sw", AgentRecord("first", "eng", "e", harness="claude", account="spent"))
    key = storage.key("sw", "quota-capacity")
    storage.redis.set(key, json.dumps({"accounts": [account().__dict__]}))
    assert quota_handoff.warn("sw", storage, {}) == ["early quota handoff warning sent to first"]
    storage.redis.set(key, json.dumps({"accounts": [account(week=0).__dict__]}))
    assert quota_handoff.warn("sw", storage, {}) == []
    storage.redis.set(key, json.dumps({"accounts": [account().__dict__]}))
    assert quota_handoff.warn("sw", storage, {}) == []
    storage.put_agent("sw", AgentRecord("second", "eng", "f", harness="claude", account="spent"))
    assert quota_handoff.warn("sw", storage, {}) == ["early quota handoff warning sent to second"]
    assert len(InboxStore(storage.redis).pending_items("first")) == 1
    assert len(InboxStore(storage.redis).pending_items("second")) == 1


def test_a_restarted_agent_gets_a_new_warning_for_its_new_life():
    import fakeredis

    storage = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    row = AgentRecord("same", "eng", "a", harness="claude", account="spent", started_at=1)
    storage.put_agent("sw", row)
    storage.redis.set(storage.key("sw", "quota-capacity"), json.dumps({"accounts": [account().__dict__]}))
    assert quota_handoff.warn("sw", storage, {}) == ["early quota handoff warning sent to same"]
    assert quota_handoff.warn("sw", storage, {}) == []
    (first,) = InboxStore(storage.redis).pending_items("same")
    assert storage.redis.hget(storage.key("sw", "quota-warnings"), "same") == first.id
    storage.put_agent("sw", replace(row, started_at=2))
    assert quota_handoff.warn("sw", storage, {}) == ["early quota handoff warning sent to same"]
    assert quota_handoff.warn("sw", storage, {}) == []
    messages = InboxStore(storage.redis).pending_items("same")
    assert len(messages) == 2
    latest = storage.redis.hget(storage.key("sw", "quota-warnings"), "same")
    assert latest != first.id
    assert latest in {item.id for item in messages}


def test_an_old_warning_does_not_suppress_a_new_lifes_quota_notice():
    import fakeredis

    storage = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    row = AgentRecord("same", "eng", "a", harness="claude", account="spent", state="working", started_at=1)
    storage.put_agent("sw", row)
    storage.redis.set(storage.key("sw", "quota-capacity"), json.dumps({"accounts": [account().__dict__]}))
    assert quota_handoff.warn("sw", storage, {}) == ["early quota handoff warning sent to same"]
    assert quota_notice.apply("sw", storage, {"accounts": [account().__dict__]}) == []
    storage.put_agent("sw", replace(row, started_at=2))
    observation = account(five=85, week=50).__dict__
    assert quota_notice.apply("sw", storage, {"accounts": [observation]}) == ["sent same the quota hurry"]


@pytest.mark.parametrize(
    "five_left,week_left,expected",
    [(None, 10, "week"), (5, None, "five hour"), (None, None, ""), (None, 50, ""), (50, None, "")],
)
def test_unknown_quota_does_not_hide_an_observed_warning_window(five_left, week_left, expected):
    row = replace(account(), five_left=five_left, week_left=week_left)
    assert quota_handoff.trigger(row, quota_handoff.Thresholds()) == expected


def test_tick_warns_once_per_agent_without_retiring_it(monkeypatch):
    import fakeredis

    storage = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0, state="running", code="a1b2c3")
    storage.create(config)
    agent = AgentRecord("engineer@a1b2c3-0001", "eng", "e", harness="claude", account="spent", state="working")
    storage.put_agent("sw", agent)
    storage.claim("sw", "e", agent.name, 60_000)
    ledger = FakeLedger([{"id": "e", "state": "claimed", "claimed_by": agent.name}])
    ledger.comment = lambda *args, **kwargs: None
    rt = FakeRuntime()
    rt.live.add(agent.name)
    rt.quota_capacity = lambda cfg, agents, now, demand, requirements: capacity.calculate(
        cfg, [account()], agents, demand, requirements
    )
    tick("sw", storage, ledger, rt, 1000)
    tick("sw", storage, ledger, rt, 2000)
    messages = InboxStore(storage.redis).pending_items(agent.name)
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
    assert storage.agents("sw")[0].name == agent.name


def test_warning_matches_account_and_harness_and_skips_finished_agents():
    import fakeredis

    storage = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    for name, harness, state in [("healthy", "claude", "working"), ("finished", "codex", "finished")]:
        storage.put_agent("sw", AgentRecord(name, "eng", "e", harness=harness, account="spent", state=state))
    storage.redis.set(
        storage.key("sw", "quota-capacity"), json.dumps({"accounts": [account(harness="codex").__dict__]})
    )
    assert quota_handoff.warn("sw", storage, {}) == []
    assert not InboxStore(storage.redis).pending_items("healthy")
    assert not InboxStore(storage.redis).pending_items("finished")


def test_a_finished_agent_on_the_warned_account_does_not_receive_a_directive():
    import fakeredis

    storage = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    storage.put_agent("sw", AgentRecord("finished", "eng", "a", harness="claude", account="spent", state="finished"))
    storage.put_agent("sw", AgentRecord("running", "eng", "b", harness="claude", account="spent", state="working"))
    storage.redis.set(storage.key("sw", "quota-capacity"), json.dumps({"accounts": [account().__dict__]}))
    assert quota_handoff.warn("sw", storage, {}) == ["early quota handoff warning sent to running"]
    assert InboxStore(storage.redis).pending_items("finished") == []
    assert len(InboxStore(storage.redis).pending_items("running")) == 1


def test_warning_ignores_unknown_state_and_handles_empty_observations():
    import fakeredis

    storage = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    storage.put_agent("sw", AgentRecord("first", "eng", "a", harness="claude", account="unobserved"))
    assert quota_handoff.warn("sw", storage, {}) == []
    storage.redis.set(
        storage.key("sw", "quota-capacity"), json.dumps({"accounts": [account(state="UNKNOWN").__dict__]})
    )
    assert quota_handoff.trigger(account(state="UNKNOWN"), quota_handoff.Thresholds()) == ""
    assert quota_handoff.warn("sw", storage, {}) == []
    storage.put_agent("sw", AgentRecord("second", "eng", "b", harness="claude", account="spent"))
    storage.redis.set(storage.key("sw", "quota-capacity"), json.dumps({"accounts": [account().__dict__]}))
    assert quota_handoff.warn("sw", storage, {}) == ["early quota handoff warning sent to second"]
    (item,) = InboxStore(storage.redis).pending_items("second")
    assert item.sender == "swarm"


def test_required_claude_successor_uses_an_eligible_account():
    row = account("healthy", five=10, week=20)
    assert quota_handoff.successor([row], False, quota_handoff.Thresholds()) == row


def test_an_eligible_weekly_only_codex_account_can_receive_the_successor():
    row = replace(account("default", "codex", week=20), five_left=None)
    assert quota_handoff.successor([row], True, quota_handoff.Thresholds()) == row
    assert quota_handoff.successor([replace(row, week_left=None)], True, quota_handoff.Thresholds()) is None
    assert quota_handoff.successor([replace(row, state="UNKNOWN")], True, quota_handoff.Thresholds()) is None


def test_quota_transfer_obeys_its_allocation_without_a_task_pin(tmp_path, monkeypatch):
    rt = runtime.HerdrRuntime(home=tmp_path)
    rt._quota_accounts = [account("cc", five=0, week=0), account("cx", "codex", five=10, week=20)]
    rt._quota_allocations = {"eng": {"claude": 0, "codex": 1}}
    monkeypatch.setattr(runtime.plugins, "claude_only", lambda _: False)
    saved = {"profile": "engineer", "harness": "claude", "model": "opus", "effort": "high", "account": "old"}
    assert rt._quota_transfer(saved, "engineer", {}, "eng", "") == {
        **saved,
        "harness": "codex",
        "model": "",
        "account": "cx",
    }


def test_quota_transfer_excludes_the_original_account_even_if_it_has_quota(tmp_path, monkeypatch):
    rt = runtime.HerdrRuntime(home=tmp_path)
    rt._quota_accounts = [account("old", five=0, week=0), account("fresh", five=10, week=20)]
    monkeypatch.setattr(runtime.plugins, "claude_only", lambda _: False)
    saved = {"profile": "engineer", "harness": "claude", "model": "opus", "effort": "high", "account": "old"}
    assert rt._quota_transfer(saved, "engineer", {}, "eng", "") == {**saved, "account": "fresh"}


def test_saved_harness_selection_keeps_its_routing_environment(tmp_path, monkeypatch):
    environ = {"AGENTIHOOKS_AGENT_PRIORITY": "codex,claude"}
    seen = []
    rt = runtime.HerdrRuntime(
        home=tmp_path, choose=lambda agent, env: seen.append((agent, env)) or (agent, "requested")
    )
    monkeypatch.setattr(runtime.plugins, "claude_only", lambda _: False)
    assert rt._saved_choice({"harness": "claude"}, "engineer", False, environ) == ("claude", "requested")
    assert seen == [("claude", environ)]
    assert rt._saved_choice({"harness": "codex"}, "engineer", True, environ) == ("codex", "quota handoff")
    assert seen == [("claude", environ)]


def test_handoff_spawn_keeps_the_router_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_AGENT_PRIORITY", "codex,claude")
    seen = []
    rt = runtime.HerdrRuntime(
        home=tmp_path,
        choose=lambda harness, environ: seen.append(environ["AGENTIHOOKS_AGENT_PRIORITY"]) or (harness, "requested"),
    )
    monkeypatch.setattr(runtime.plugins, "claude_only", lambda _: False)
    monkeypatch.setattr(rt, "_launch", lambda *args, **kwargs: runtime.Placed("pane", "claude", "old"))
    saved = {"profile": "engineer", "harness": "claude", "model": "opus", "effort": "high", "account": "old"}
    task = {
        "id": "e",
        "title": "Continue",
        "handoff": "Saved",
        "handoff_envelope": {"reason": "recycle", "launch": saved},
    }
    rt.spawn(SwarmConfig("sw", str(tmp_path), max_eng=1, max_ci=0, code="a1b2c3"), "eng", "engineer@a1b2c3-0002", task)
    assert seen == ["codex,claude"]


@pytest.mark.parametrize("state", ["DRAIN", "DRAIN_SOON", "REDUCE"])
def test_successor_uses_most_routing_left_and_skips_unplaceable_accounts(state):
    rows = [
        account("old", week=91),
        account("blocked", five=0, week=0, state=state, sessions=3),
        account("middle", five=30, week=40),
        account("best", five=10, week=20),
        account("cx", "codex", five=0, week=0),
    ]
    assert quota_handoff.successor(rows, True, quota_handoff.Thresholds()).name == "best"


def test_successor_falls_back_to_codex_and_respects_profile_and_floor():
    rows = [account("cc", state="DRAIN"), account("cx", "codex", five=10, week=20)]
    assert quota_handoff.successor(rows, True, quota_handoff.Thresholds()) == rows[1]
    assert quota_handoff.successor(rows, False, quota_handoff.Thresholds()) is None
    assert quota_handoff.successor([replace(rows[1], cap=0)], True, quota_handoff.Thresholds()) is None


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


@pytest.mark.parametrize("same_lane,pinned", [(False, False), (False, True), (True, True)])
def test_quota_successor_uses_its_lane_reservation(tmp_path, monkeypatch, same_lane, pinned):
    rt = runtime.HerdrRuntime(home=tmp_path, choose=lambda *_: ("claude", "priority"))
    rt._quota_accounts = [account("cc", five=0, week=0), account("cx", "codex", five=10, week=20)]
    rt._quota_cap, rt._quota_floor, rt._quota_share = 3, 5, 30
    rt._quota_allocations = {
        "eng": {"claude": int(same_lane), "codex": 1},
        "ci": {"claude": int(not same_lane), "codex": 0},
    }
    rt._quota_tasks = {"e": "codex", "required": "claude"} if pinned else {}
    seen = []
    monkeypatch.setattr(runtime.plugins, "claude_only", lambda _: False)
    monkeypatch.setattr(
        rt,
        "_launch",
        lambda cfg, lane, task, name, argv, **kw: seen.append(argv) or runtime.Placed("pane", "codex", "cx"),
    )
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
    assert _option(seen[0], "--agent") == "codex"
    assert rt._quota_allocations == {
        "eng": {"claude": int(same_lane), "codex": 0},
        "ci": {"claude": int(not same_lane), "codex": 0},
    }


CODEX_HANDING_OFF = {"profile": "engineer", "harness": "codex", "account": "default", "model": "gpt", "effort": "high"}


def _codex_handing_off_rows(claude_week_used=20):
    return [
        capacity.Account("claude", "cc", "OPEN", 2, 90, 100 - claude_week_used, 6),
        capacity.Account("codex", "default", "OPEN", 0, None, 9, 6),
    ]


def _quota_decision(tmp_path, monkeypatch, rows, launch, tasks, lanes=None):
    monkeypatch.setattr(capacity, "accounts", lambda environ, now, refresh=True: list(rows))
    monkeypatch.setattr(runtime.plugins, "claude_only", lambda _: False)
    rt = runtime.HerdrRuntime(home=tmp_path)
    config = SwarmConfig("sw", str(tmp_path), max_eng=len(tasks), max_ci=0, max_plan=0, lanes=lanes or {})
    envelope = {"reason": "quota", "launch": launch}
    ready = {"eng": [{"id": task, "handoff_envelope": envelope} for task in tasks], "ci": [], "plan": []}
    requirements = rt.quota_requirements(config, ready)
    return rt.quota_capacity(config, [], 100, {"eng": len(tasks), "ci": 0, "plan": 0}, requirements)


@pytest.mark.parametrize("claude_week_used,expected", [(20, {"q": "claude"}), (95, {})])
def test_quota_capacity_places_a_quota_handoff_only_on_a_harness_with_an_eligible_successor(
    tmp_path, monkeypatch, claude_week_used, expected
):
    rows = _codex_handing_off_rows(claude_week_used)
    decision = _quota_decision(tmp_path, monkeypatch, rows, CODEX_HANDING_OFF, ["q"])
    assert decision["tasks"] == expected


def test_quota_capacity_names_why_a_quota_handoff_waits(tmp_path, monkeypatch):
    decision = _quota_decision(tmp_path, monkeypatch, _codex_handing_off_rows(95), CODEX_HANDING_OFF, ["q"])
    assert decision["reason"].endswith(
        "; quota handoff q waits: no claude or codex account can take the quota handoff from codex account default: "
        "claude cc is at its week quota warning; codex default is the account handing off"
    )


def test_quota_capacity_reserves_each_quota_handoff_on_an_eligible_account(tmp_path, monkeypatch):
    rows = [
        capacity.Account("claude", "warn", "OPEN", 0, 90, 8, 6),
        capacity.Account("claude", "ok", "OPEN", 5, 90, 80, 6),
    ]
    launch = {**CODEX_HANDING_OFF, "harness": "claude", "account": "gone"}
    decision = _quota_decision(tmp_path, monkeypatch, rows, launch, ["q1", "q2"], {"eng": {"agent": "claude"}})
    assert decision["tasks"] == {"q1": "claude"}
    assert decision["placements"]["eng"] == [{"index": 0, "harness": "claude", "account": "ok"}]


def test_quota_transfer_prefers_its_planned_harness_but_falls_back_to_an_eligible_one(tmp_path, monkeypatch):
    rt = runtime.HerdrRuntime(home=tmp_path)
    rt._quota_accounts = _codex_handing_off_rows()
    rt._quota_allocations = {"eng": {"claude": 1, "codex": 1}}
    monkeypatch.setattr(runtime.plugins, "claude_only", lambda _: False)
    assert rt._quota_transfer(CODEX_HANDING_OFF, "engineer", {}, "eng", "", ("codex", "default")) == {
        **CODEX_HANDING_OFF,
        "harness": "claude",
        "account": "cc",
        "model": "",
    }


def test_quota_transfer_takes_the_account_capacity_reserved(tmp_path, monkeypatch):
    rt = runtime.HerdrRuntime(home=tmp_path)
    rt._quota_accounts = [*_codex_handing_off_rows(), capacity.Account("claude", "reserved", "OPEN", 5, 50, 50, 6)]
    monkeypatch.setattr(runtime.plugins, "claude_only", lambda _: False)
    chosen = rt._quota_transfer(CODEX_HANDING_OFF, "engineer", {}, "eng", "", ("claude", "reserved"))
    assert (chosen["harness"], chosen["account"]) == ("claude", "reserved")


def test_quota_transfer_refusal_names_the_predecessor_and_why_each_account_was_excluded(tmp_path, monkeypatch):
    rt = runtime.HerdrRuntime(home=tmp_path)
    rt._quota_accounts = [*_codex_handing_off_rows(95), capacity.Account("claude", "full", "OPEN", 6, 90, 90, 6)]
    rt._quota_allocations = {"eng": {"claude": 1, "codex": 1}}
    monkeypatch.setattr(runtime.plugins, "claude_only", lambda _: False)
    with pytest.raises(runtime.SpawnError) as refused:
        rt._quota_transfer(CODEX_HANDING_OFF, "engineer", {}, "eng", "", ("codex", "default"))
    assert str(refused.value) == (
        "no claude or codex account can take the quota handoff from codex account default: "
        "claude cc is at its week quota warning; codex default is the account handing off; "
        "claude full has no free seats"
    )
