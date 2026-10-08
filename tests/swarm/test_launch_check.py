import json
from dataclasses import replace

import pytest

from hooks.context import profile_chain
from scripts.swarm import launch_check, master_start
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeLedger, FakeRuntime

pytestmark = pytest.mark.xdist_group("fakeredis")

LAUNCH = 1_000_000
LAUNCH_CHECKED = True


@pytest.fixture
def store():
    import fakeredis

    s = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    s.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0))
    return s


@pytest.fixture
def launched(store, tmp_path):
    code = store.ensure_code("sw").code
    home = tmp_path / "engineer" / "claude"
    home.mkdir(parents=True)
    (home / ".agentihooks-render.json").write_text(
        json.dumps({"chain": ["base", "package:engineer", "engineer", "brain"], "overlays": ["brain"]})
    )
    agent = AgentRecord(
        f"engineer@{code}-0001",
        "eng",
        "t1",
        harness="claude",
        profile="engineer",
        model="opus",
        effort="high",
        seat="eng-1@sw",
        started_at=LAUNCH,
        profile_decision={"validation": {"home": str(home), "model": "opus", "effort": "high"}},
    )
    store.put_agent("sw", agent)
    store.seats.occupy(agent.seat, agent.name, LAUNCH)
    facts = {
        "harness": "claude",
        "home": str(home),
        "profile": "engineer",
        "model": "opus",
        "effort": "high",
        "account": "",
        "hooks": True,
        "chain": launch_check.chain(home),
        "overlays": profile_chain.rendered_overlays(home),
    }
    doc = {"_meta": {"members": {agent.name: {"joined_at": LAUNCH + 20_000}}}}
    return agent, facts, doc


def misses(store, agent, facts, doc, bundled=True, declared=()):
    return launch_check.misses(store, "sw", agent, facts, doc, bundled, list(declared))


def test_a_clean_launch_passes(store, launched):
    agent, facts, doc = launched
    assert misses(store, agent, facts, doc) == {}


def test_not_joined_is_named(store, launched):
    agent, facts, _ = launched
    assert list(misses(store, agent, facts, {"_meta": {"members": {}}})) == ["joined"]


def test_a_seated_join_after_the_deadline_is_named_late(store, launched):
    agent, facts, doc = launched
    doc["_meta"]["members"][agent.name]["joined_at"] = LAUNCH + launch_check.DEADLINE_MS + 1
    assert list(misses(store, agent, facts, doc)) == ["late"]
    assert "late" in launch_check.REPORT_ONLY


def test_the_join_deadline_starts_at_the_harness_start(store, launched):
    agent, facts, doc = launched
    agent = replace(agent, launched_at=LAUNCH, launch_timings={"harness_at": LAUNCH + 30_000})
    doc["_meta"]["members"][agent.name]["joined_at"] = LAUNCH + 89_000
    assert misses(store, agent, facts, doc) == {}
    doc["_meta"]["members"][agent.name]["joined_at"] = LAUNCH + 91_000
    assert misses(store, agent, facts, doc)["late"]["actual"]["joined_after_ms"] == 61_000


def test_a_harness_start_before_the_launch_is_ignored(store, launched):
    agent, facts, doc = launched
    agent = replace(agent, launched_at=LAUNCH, launch_timings={"harness_at": LAUNCH - 30_000})
    assert launch_check.session_started_at(agent) == LAUNCH


def test_seat_held_by_another_is_named(store, launched):
    agent, facts, doc = launched
    store.seats.occupy(agent.seat, "someone-else", LAUNCH + 1)
    assert list(misses(store, agent, facts, doc)) == ["joined"]


def test_wrong_profile_variable_is_named(store, launched):
    agent, facts, doc = launched
    found = misses(store, agent, {**facts, "profile": "anton"}, doc)
    assert found == {"profile": {"expected": "engineer", "actual": "anton"}}


@pytest.mark.parametrize("field,value", [("hooks", False), ("model", "sonnet"), ("effort", "low")])
def test_missing_hooks_or_wrong_model_or_effort_is_named_settings(store, launched, field, value):
    agent, facts, doc = launched
    found = misses(store, agent, {**facts, field: value}, doc)
    assert list(found) == ["settings"]
    assert found["settings"]["actual"][field] == value


def test_no_live_process_misses_profile_and_settings(store, launched):
    agent, _, doc = launched
    assert list(misses(store, agent, {"process": False}, doc)) == ["profile", "settings", "base"]
    assert list(misses(store, agent, {"process": False}, doc, declared=["brain"])) == [
        "profile",
        "settings",
        "base",
        "overlay",
    ]


@pytest.mark.parametrize(
    "chain",
    [[], ["base", "engineer"], ["package:engineer", "engineer", "extra"], ["package:engineer"]],
)
def test_role_not_on_its_package_base_role_is_named_base(store, launched, chain):
    agent, facts, doc = launched
    assert list(misses(store, agent, {**facts, "chain": chain, "overlays": []}, doc)) == ["base"]


def test_package_role_without_a_bundle_overlay_passes(store, launched):
    agent, facts, doc = launched
    assert misses(store, agent, {**facts, "chain": ["engineer"]}, doc, bundled=False) == {}


@pytest.mark.parametrize("name", ["engineer-1", "ci@{code}-0001", "engineer@abcdef-0001"])
def test_name_not_parsing_to_lane_code_and_number_is_named(store, launched, name):
    agent, facts, doc = launched
    renamed = replace(agent, name=name.format(code=store.config("sw").code))
    store.seats.occupy(agent.seat, renamed.name, LAUNCH)
    doc["_meta"]["members"] = {renamed.name: {"joined_at": LAUNCH}}
    assert list(misses(store, renamed, facts, doc)) == ["name"]


@pytest.mark.parametrize("wrapped", [False, True])
def test_chain_reads_the_rendered_stamp(tmp_path, wrapped):
    stamp = tmp_path / ".agentihooks-render.json"
    assert launch_check.chain(tmp_path) == []
    data = {"chain": ["a", "b"]}
    stamp.write_text(json.dumps({"render": data, "operator": "digest"} if wrapped else data))
    assert launch_check.chain(tmp_path) == ["a", "b"]
    for data in ({}, {"chain": None}, [], None, {"render": {}}, {"render": None}, {"render": []}):
        stamp.write_text(json.dumps(data))
        assert launch_check.chain(tmp_path) == []
    stamp.write_text("{")
    assert launch_check.chain(tmp_path) == []


def test_a_launch_with_every_declared_overlay_passes(store, launched):
    agent, facts, doc = launched
    assert misses(store, agent, facts, doc, declared=["brain"]) == {}


@pytest.mark.parametrize(
    "rendered,declared",
    [([], ["brain"]), (["router"], ["brain", "router"]), (None, ["brain"])],
)
def test_a_launch_missing_a_declared_overlay_fails_the_check(store, launched, rendered, declared):
    agent, facts, doc = launched
    chain = ["base", "package:engineer", "engineer", *(rendered or [])]
    found = misses(store, agent, {**facts, "chain": chain, "overlays": rendered}, doc, declared=declared)
    assert found == {"overlay": {"expected": declared, "actual": rendered or []}}


def test_declared_reads_the_overlays_render_would_layer(monkeypatch):
    from scripts.profiles import render

    monkeypatch.setattr(render, "declared", lambda profile: [f"{profile}-overlay"])
    assert launch_check.declared("engineer") == ["engineer-overlay"]


def test_declared_adds_the_overlays_the_agent_wears(monkeypatch):
    from scripts.profiles import render

    monkeypatch.setattr(render, "declared", lambda profile: ["brain"])
    assert launch_check.declared("engineer", ["tuner", "trader"]) == ["brain", "tuner", "trader"]
    assert launch_check.declared("engineer", ["brain"]) == ["brain"]


def test_a_launch_missing_a_worn_overlay_fails_the_check(store, launched):
    agent, facts, doc = launched
    chain = ["base", "package:engineer", "engineer", "brain"]
    worn = {**facts, "chain": chain, "overlays": ["brain"]}
    assert misses(store, agent, worn, doc, declared=["brain", "tuner"]) == {
        "overlay": {"expected": ["brain", "tuner"], "actual": ["brain"]}
    }
    worn = {**facts, "chain": [*chain, "tuner"], "overlays": ["brain", "tuner"]}
    assert misses(store, agent, worn, doc, declared=["brain", "tuner"]) == {}


class JoiningLedger(FakeLedger):
    def __init__(self, tasks):
        super().__init__(tasks)
        self.members = {}

    def state(self, slug):
        doc = super().state(slug)
        doc["_meta"]["members"] = self.members
        return doc


class CheckedRuntime(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.profile = "engineer"

    def bindings(self, agents):
        facts = super().bindings(agents)
        for agent in agents:
            if agent.name in facts:
                profile = agent.profile if agent.lane == MASTER else self.profile
                facts[agent.name].update(profile=profile, chain=[agent.profile])
        return facts


def checked(store, monkeypatch, profile="engineer", task_profile="engineer"):
    from hooks.context import profile_chain

    monkeypatch.setattr(profile_chain, "read_state", lambda: {})
    monkeypatch.setattr(launch_check, "declared", lambda profile, worn=(): [])
    monkeypatch.setenv("AGENTIHOOKS_MASTER_RETIRE_HANDOFF_MINUTES", "0")
    ledger, runtime = JoiningLedger([{"id": "t1", "profile": task_profile}]), CheckedRuntime()
    original = runtime.spawn

    def spawn(config, lane, name, task):
        placed = original(config, lane, name, task)
        return replace(placed, profile=profile)

    runtime.spawn = spawn
    return ledger, runtime


def joined(ledger, runtime, at):
    for name in [n for n, _ in runtime.masters] + [n for _, n, _ in runtime.spawned]:
        ledger.members.setdefault(name, {"joined_at": at})


def test_tick_passes_a_launch_that_joined(store, monkeypatch):
    ledger, runtime = checked(store, monkeypatch)
    tick("sw", store, ledger, runtime, LAUNCH)
    (name,) = [n for _, n, _ in runtime.spawned]
    joined(ledger, runtime, LAUNCH + 15_000)
    actions = tick("sw", store, ledger, runtime, LAUNCH + 30_000)
    assert f"{name} passed its launch check in 15 seconds" in actions
    assert launch_check.pending(store, "sw") == {}
    assert launch_check.findings(store, "sw") == []
    assert launch_check.report(store, "sw", "t1")["state"] == "passed"


@pytest.mark.parametrize("check_delay", [199_437, 286_137, 86_400_000])
def test_delayed_tick_passes_a_joined_agent_after_leave(store, monkeypatch, check_delay):
    ledger, runtime = checked(store, monkeypatch)
    tick("sw", store, ledger, runtime, LAUNCH)
    (name,) = [n for _, n, _ in runtime.spawned]
    joined(ledger, runtime, LAUNCH + 15_000)
    ledger.members.pop(name)
    original = ledger.state

    def state(slug):
        doc = original(slug)
        doc["_meta"]["events"] = [
            {"kind": "joined", "by": name, "at": LAUNCH + 15_000},
            {"kind": "left", "by": name, "at": LAUNCH + 100_000},
        ]
        return doc

    ledger.state = state
    actions = tick("sw", store, ledger, runtime, LAUNCH + check_delay)
    assert f"{name} passed its launch check in 15 seconds" in actions
    report = launch_check.report(store, "sw", "t1")
    assert report["state"] == "passed"
    assert report["elapsed_ms"] == 15_000
    assert launch_check.pending(store, "sw") == {}


def test_the_join_clock_starts_at_the_launch_not_at_the_tick(store, monkeypatch):
    ledger, runtime = checked(store, monkeypatch)
    spawn = runtime.spawn

    def late(config, lane, name, task):
        return replace(spawn(config, lane, name, task), launched_at=LAUNCH + 50_000)

    runtime.spawn = late
    tick("sw", store, ledger, runtime, LAUNCH)
    (name,) = [n for _, n, _ in runtime.spawned]
    joined(ledger, runtime, LAUNCH + 100_000)
    actions = tick("sw", store, ledger, runtime, LAUNCH + 105_000)
    assert f"{name} passed its launch check in 50 seconds" in actions


def test_the_tick_times_the_join_from_the_harness_start(store, monkeypatch):
    ledger, runtime = checked(store, monkeypatch)
    spawn = runtime.spawn

    def slow_launcher(config, lane, name, task):
        placed = replace(spawn(config, lane, name, task), launched_at=LAUNCH)
        return replace(placed, launch_timings={"launched_at": LAUNCH, "harness_at": LAUNCH + 30_000})

    runtime.spawn = slow_launcher
    tick("sw", store, ledger, runtime, LAUNCH)
    (name,) = [n for _, n, _ in runtime.spawned]
    waiting = tick("sw", store, ledger, runtime, LAUNCH + 70_000)
    assert not any(name in a and "launch check" in a for a in waiting)
    joined(ledger, runtime, LAUNCH + 80_000)
    actions = tick("sw", store, ledger, runtime, LAUNCH + 85_000)
    assert f"{name} passed its launch check in 50 seconds" in actions
    assert name not in runtime.killed


def test_a_late_join_that_holds_its_seat_is_reported_and_kept(store, monkeypatch):
    ledger, runtime = checked(store, monkeypatch)
    tick("sw", store, ledger, runtime, LAUNCH)
    (name,) = [n for _, n, _ in runtime.spawned]
    joined(ledger, runtime, LAUNCH + 71_000)
    actions = tick("sw", store, ledger, runtime, LAUNCH + 120_000)
    assert f"{name} failed its launch check on late; reported only" in actions
    assert name not in runtime.killed
    assert len(runtime.spawned) == 1
    report = launch_check.report(store, "sw", "t1")
    assert report["state"] == "reported"
    assert report["misses"]["late"]["actual"] == {"joined_after_ms": 71_000, "seat": "eng-1@sw"}
    assert launch_check.findings(store, "sw") == []
    assert name not in launch_check.pending(store, "sw")


def test_an_agent_that_never_joins_is_still_retired_and_relaunched(store, monkeypatch, scratch):
    scratch("t1")
    ledger, runtime = checked(store, monkeypatch)
    tick("sw", store, ledger, runtime, LAUNCH)
    (name,) = [n for _, n, _ in runtime.spawned]
    actions = tick("sw", store, ledger, runtime, LAUNCH + launch_check.DEADLINE_MS)
    assert any(a.startswith(f"retired {name} after its launch check failed on joined") for a in actions)
    assert name in runtime.killed
    assert len(runtime.spawned) == 2


def test_a_failed_launch_names_every_miss_and_times_from_the_launch(store, monkeypatch, scratch):
    scratch("t1")
    ledger, runtime = checked(store, monkeypatch)
    runtime.profile = "anton"
    spawn = runtime.spawn

    def late(config, lane, name, task):
        return replace(spawn(config, lane, name, task), launched_at=LAUNCH + 50_000)

    runtime.spawn = late
    tick("sw", store, ledger, runtime, LAUNCH)
    (name,) = [n for _, n, _ in runtime.spawned]
    waiting = tick("sw", store, ledger, runtime, LAUNCH + launch_check.DEADLINE_MS)
    assert not any(name in a and "launch check" in a for a in waiting)
    actions = tick("sw", store, ledger, runtime, LAUNCH + 50_000 + launch_check.DEADLINE_MS)
    assert any(a.startswith(f"retired {name} after its launch check failed on joined, profile") for a in actions)
    assert launch_check.report(store, "sw", "t1")["elapsed_ms"] == launch_check.DEADLINE_MS


def test_tick_waits_until_the_deadline_before_failing(store, monkeypatch):
    ledger, runtime = checked(store, monkeypatch)
    tick("sw", store, ledger, runtime, LAUNCH)
    actions = tick("sw", store, ledger, runtime, LAUNCH + 30_000)
    assert not any("launch check" in a for a in actions)
    (name,) = [n for _, n, _ in runtime.spawned]
    master = runtime.masters[0][0]
    assert launch_check.pending(store, "sw") == {
        name: {"task": "t1", "at": LAUNCH, "relaunch": True},
        master: {"task": MASTER, "at": LAUNCH, "relaunch": True},
    }


def test_tick_retires_and_relaunches_a_failed_launch_once(store, monkeypatch, scratch):
    homes = scratch("t1")
    ledger, runtime = checked(store, monkeypatch)
    runtime.profile = "anton"
    tick("sw", store, ledger, runtime, LAUNCH)
    first = runtime.spawned[0][1]
    joined(ledger, runtime, LAUNCH + 1)
    actions = tick("sw", store, ledger, runtime, LAUNCH + launch_check.DEADLINE_MS)
    assert any(a.startswith(f"retired {first} after its launch check failed on profile") for a in actions)
    assert first in runtime.killed
    assert runtime.homes[first] == homes
    failed = launch_check.report(store, "sw", "t1")
    assert (failed["at"], failed["elapsed_ms"], failed["held"]) == (LAUNCH + 60_000, 60_000, False)
    assert runtime.tasks[1]["launch_assignment"]["profile"] == "engineer"
    (finding,) = launch_check.findings(store, "sw")
    assert finding.id == f"launch-check/{first}/profile"
    second = runtime.spawned[1][1]
    assert runtime.spawned[1][2] == "t1"
    joined(ledger, runtime, LAUNCH + launch_check.DEADLINE_MS + 1)
    later = LAUNCH + 2 * launch_check.DEADLINE_MS
    actions = tick("sw", store, ledger, runtime, later)
    assert f"{second} failed its launch check on profile; its one relaunch is spent" in actions
    assert second not in runtime.killed
    assert len(runtime.spawned) == 2
    assert [f.id for f in launch_check.findings(store, "sw")] == [f"launch-check/{second}/profile"]


def test_master_failure_is_posted_to_the_operator_chat_and_relaunched(store, monkeypatch):
    ledger, runtime = checked(store, monkeypatch)
    store.update("sw", state="running")
    store.update("sw", max_eng=0)
    tick("sw", store, ledger, runtime, LAUNCH)
    (first, _) = runtime.masters[0]
    actions = tick("sw", store, ledger, runtime, LAUNCH + launch_check.DEADLINE_MS)
    assert any(a.startswith(f"retired {first} after its launch check failed on joined") for a in actions)
    assert any("master" in note and "joining the ledger" in note for note in ledger.notes)
    assert [a.name for a in store.agents("sw") if a.lane == MASTER and a.name == first] == []
    assert master_start.read(store, "sw").get("name", "") in ("", runtime.masters[-1][0])
    tick("sw", store, ledger, runtime, LAUNCH + launch_check.DEADLINE_MS + 60_000)
    assert len(runtime.masters) == 2


def test_master_notification_names_every_miss(store, monkeypatch, tmp_path):
    from hooks.context import profile_chain

    ledger, runtime = checked(store, monkeypatch)
    monkeypatch.setattr(profile_chain, "read_state", lambda: {"bundle": {"path": str(tmp_path)}})
    store.update("sw", state="running")
    store.update("sw", max_eng=0)
    tick("sw", store, ledger, runtime, LAUNCH)
    first, _ = runtime.masters[0]
    agent = next(a for a in store.agents("sw") if a.name == first)
    (tmp_path / "profiles" / agent.profile).mkdir(parents=True)
    tick("sw", store, ledger, runtime, LAUNCH + launch_check.DEADLINE_MS)
    assert ledger.notes == [
        "The master failed its launch check within a minute on joining the ledger and holding its seat, "
        "its role on the package base role. It is being retired and relaunched once."
    ]
    assert [f.id for f in launch_check.findings(store, "sw")] == [
        f"launch-check/{first}/joined",
        f"launch-check/{first}/base",
    ]
    assert list(launch_check.report(store, "sw", "master")["misses"]) == ["joined", "base"]


def test_a_wrong_base_retires_and_relaunches_the_launch(store, monkeypatch, tmp_path, scratch):
    from hooks.context import profile_chain

    scratch("t1")
    ledger, runtime = checked(store, monkeypatch)
    (tmp_path / "profiles" / "engineer").mkdir(parents=True)
    monkeypatch.setattr(profile_chain, "read_state", lambda: {"bundle": {"path": str(tmp_path)}})
    tick("sw", store, ledger, runtime, LAUNCH)
    first = runtime.spawned[0][1]
    joined(ledger, runtime, LAUNCH + 1)
    actions = tick("sw", store, ledger, runtime, LAUNCH + launch_check.DEADLINE_MS)
    assert any(a.startswith(f"retired {first} after its launch check failed on base") for a in actions)
    assert first in runtime.killed
    assert [f.id for f in launch_check.findings(store, "sw") if f.subject.startswith(first)] == [
        f"launch-check/{first}/base"
    ]
    assert list(launch_check.report(store, "sw", "t1")["misses"]) == ["base"]
    assert launch_check.relaunched(store, "sw", "t1") is True


def test_tick_retires_and_relaunches_a_launch_missing_a_declared_overlay(store, monkeypatch, scratch):
    scratch("t1")
    ledger, runtime = checked(store, monkeypatch)
    monkeypatch.setattr(launch_check, "declared", lambda profile, worn=(): ["brain"])
    tick("sw", store, ledger, runtime, LAUNCH)
    first = runtime.spawned[0][1]
    joined(ledger, runtime, LAUNCH + 1)
    actions = tick("sw", store, ledger, runtime, LAUNCH + launch_check.DEADLINE_MS)
    assert any(a.startswith(f"retired {first} after its launch check failed on overlay") for a in actions)
    assert first in runtime.killed
    assert launch_check.report(store, "sw", "t1")["misses"]["overlay"] == {"expected": ["brain"], "actual": []}


def test_tick_expects_the_overlays_the_agent_was_launched_wearing(store, monkeypatch, scratch):
    scratch("t1")
    ledger, runtime = checked(store, monkeypatch)
    monkeypatch.setattr(launch_check, "declared", lambda profile, worn=(): list(worn))
    original = runtime.spawn
    runtime.spawn = lambda *args, **kwargs: replace(original(*args, **kwargs), overlays=["tuner"])
    tick("sw", store, ledger, runtime, LAUNCH)
    joined(ledger, runtime, LAUNCH + 1)
    tick("sw", store, ledger, runtime, LAUNCH + launch_check.DEADLINE_MS)
    assert launch_check.report(store, "sw", "t1")["misses"]["overlay"] == {"expected": ["tuner"], "actual": []}


def test_a_take_master_launch_that_fails_is_reported_and_kept(store, monkeypatch):
    ledger, runtime = checked(store, monkeypatch)
    store.update("sw", max_eng=0)
    agent = AgentRecord(
        f"master@{store.ensure_code('sw').code}-0009", MASTER, MASTER, seat="master@sw", started_at=LAUNCH
    )
    store.put_agent("sw", agent)
    runtime.live.add(agent.name)
    launch_check.begin(store, "sw", agent, LAUNCH, relaunch=False)
    actions = tick("sw", store, ledger, runtime, LAUNCH + launch_check.DEADLINE_MS)
    assert f"{agent.name} failed its launch check on joined; reported only" in actions
    assert agent.name not in runtime.killed
    assert any("joining the ledger" in note for note in ledger.notes)


def test_take_master_begins_a_check_that_never_relaunches(store, monkeypatch, tmp_path):
    from scripts.swarm import take_master

    monkeypatch.setattr(take_master, "PROC", tmp_path / "no-proc")
    monkeypatch.setattr(take_master, "agent_pid", lambda: 4242)
    monkeypatch.setattr(take_master, "harness_of", lambda pid: "claude")
    monkeypatch.setattr(take_master, "argv_of", lambda pid: ())
    monkeypatch.setattr(take_master, "name_session", lambda pid, name: 1)
    record, _ = take_master.take(store, "sw", "", FakeRuntime(), LAUNCH)
    assert launch_check.pending(store, "sw") == {record.name: {"task": MASTER, "at": LAUNCH, "relaunch": False}}


def test_joined_at_reads_through_missing_levels(launched):
    agent, _, doc = launched
    assert launch_check.joined_at(agent, {}) is None
    assert launch_check.joined_at(agent, {"_meta": {}}) is None
    assert launch_check.joined_at(agent, {"_meta": {"members": {}}}) is None
    assert launch_check.joined_at(agent, {"_meta": {"members": {agent.name: {}}}}) is None
    assert launch_check.joined_at(agent, doc) == LAUNCH + 20_000


@pytest.mark.parametrize("delay", [23_106, 19_737])
def test_a_retained_join_passes_after_leaving(store, launched, delay):
    agent, facts, doc = launched
    doc["_meta"]["members"] = {}
    doc["_meta"]["events"] = [
        {"kind": "joined", "by": agent.name, "at": LAUNCH + delay},
        {"kind": "left", "by": agent.name, "at": LAUNCH + 160_000},
    ]
    assert misses(store, agent, facts, doc) == {}
    assert launch_check.joined_at(agent, doc) == LAUNCH + delay


@pytest.mark.parametrize("source", ["members", "events", "join_history"])
def test_a_join_from_an_earlier_launch_does_not_pass(store, launched, source):
    agent, facts, doc = launched
    old = LAUNCH - 1
    doc["_meta"] = {
        "members": {agent.name: {"joined_at": old}} if source == "members" else {},
        "events": [{"kind": "joined", "by": agent.name, "at": old}] if source == "events" else [],
        "join_history": {agent.name: [old]} if source == "join_history" else {},
    }
    assert "joined" in misses(store, agent, facts, doc)


@pytest.mark.parametrize("source", ["events", "join_history"])
@pytest.mark.parametrize("delay", [0, 60_000, 60_001])
def test_retained_join_deadline_and_identity(store, launched, source, delay):
    agent, facts, doc = launched
    doc["_meta"] = {"members": {}}
    evidence = (
        [{"kind": "joined", "by": agent.name, "at": LAUNCH + delay}]
        if source == "events"
        else {agent.name: [LAUNCH - 1, LAUNCH + delay, LAUNCH + 70_000]}
    )
    doc["_meta"][source] = evidence
    assert list(misses(store, agent, facts, doc)) == (["late"] if delay > 60_000 else [])
    assert launch_check.joined_at(agent, doc) == LAUNCH + delay
    store.seats.occupy(agent.seat, "someone-else", LAUNCH + 1)
    assert "joined" in misses(store, agent, facts, doc)
    store.seats.occupy(agent.seat, agent.name, LAUNCH + 2)
    agent = replace(agent, name=agent.name + "-other")
    assert launch_check.joined_at(agent, doc) is None
    assert "joined" in misses(store, agent, facts, doc)


def test_joined_miss_names_the_delay_and_the_seat(store, launched):
    agent, facts, doc = launched
    doc["_meta"]["members"][agent.name]["joined_at"] = LAUNCH + 61_000
    store.seats.occupy(agent.seat, "someone-else", LAUNCH + 1)
    assert misses(store, agent, facts, doc)["joined"] == {
        "expected": {"joined_within_ms": 60_000, "seat": "eng-1@sw"},
        "actual": {"joined_after_ms": 61_000, "seat": ""},
    }
    assert misses(store, agent, facts, {})["joined"]["actual"] == {"joined_after_ms": None, "seat": ""}


def test_joined_exactly_at_the_deadline_passes(store, launched):
    agent, facts, doc = launched
    doc["_meta"]["members"][agent.name]["joined_at"] = LAUNCH + 60_000
    assert misses(store, agent, facts, doc) == {}


def test_an_unseated_record_misses_joined(store, launched):
    agent, facts, doc = launched
    assert list(misses(store, replace(agent, seat=""), facts, doc)) == ["joined"]


def test_settings_miss_names_the_assigned_values(store, launched):
    agent, facts, doc = launched
    assert misses(store, agent, {**facts, "hooks": False, "effort": "low"}, doc)["settings"] == {
        "expected": {"hooks": True, "model": "opus", "effort": "high"},
        "actual": {"hooks": False, "model": "opus", "effort": "low"},
    }


def test_missing_facts_miss_profile_and_settings_with_nothing_observed(store, launched):
    agent, _, doc = launched
    found = misses(store, agent, {}, doc)
    assert found["profile"] == {"expected": "engineer", "actual": None}
    assert found["settings"]["actual"] == {"hooks": None, "model": None, "effort": None}


def test_base_miss_names_the_expected_chain(store, launched):
    agent, facts, doc = launched
    assert misses(store, agent, {**facts, "chain": ["base", "engineer", "brain"]}, doc)["base"] == {
        "expected": "package:<role>, engineer",
        "actual": ["base", "engineer"],
    }
    assert misses(store, agent, {**facts, "chain": []}, doc, bundled=False)["base"] == {
        "expected": "engineer",
        "actual": [],
    }


def test_name_miss_names_the_expected_shape(store, launched):
    agent, facts, doc = launched
    renamed = replace(agent, name="engineer-1")
    store.seats.occupy(agent.seat, renamed.name, LAUNCH)
    doc["_meta"]["members"] = {renamed.name: {"joined_at": LAUNCH}}
    assert misses(store, renamed, facts, doc)["name"] == {
        "expected": f"engineer@{store.config('sw').code}-<number>",
        "actual": "engineer-1",
    }


def test_bundled_reads_the_linked_bundle_profiles(tmp_path, monkeypatch):
    from hooks.context import profile_chain

    (tmp_path / "profiles" / "engineer").mkdir(parents=True)
    monkeypatch.setattr(profile_chain, "read_state", lambda: {"bundle": {"path": str(tmp_path)}})
    assert launch_check.bundled("engineer") is True
    assert launch_check.bundled("qa") is False
    assert launch_check.bundled("") is False
    monkeypatch.setattr(profile_chain, "read_state", lambda: {})
    assert launch_check.bundled("engineer") is False


def test_record_report_and_judged(store, launched):
    agent, _, _ = launched
    assert launch_check.report(store, "sw", "t1") == {}
    found = {"name": {"expected": "a", "actual": "b"}}
    assert launch_check.record(store, "sw", agent, found, LAUNCH + 5, 7) == {
        "agent": agent.name,
        "task": "t1",
        "at": LAUNCH + 5,
        "elapsed_ms": 7,
        "state": "failed",
        "misses": found,
        "held": False,
    }
    assert launch_check.report(store, "sw", "t1")["state"] == "failed"
    assert launch_check.judged(store, "sw") == set()
    launch_check.record(store, "sw", agent, found, LAUNCH, 0, held=True)
    other = replace(agent, name="pending-one", task="t2")
    launch_check.begin(store, "sw", other, LAUNCH)
    assert launch_check.judged(store, "sw") == {agent.name, "pending-one"}
    launch_check.record(store, "sw", agent, {}, LAUNCH, 0)
    assert launch_check.report(store, "sw", "t1")["state"] == "passed"
    assert launch_check.judged(store, "sw") == {"pending-one"}
    launch_check.forget(store, "sw", "pending-one")
    assert launch_check.pending(store, "sw") == {}


def test_relaunch_mark_is_set_and_cleared_per_task(store):
    launch_check.mark_relaunched(store, "sw", "t1")
    assert launch_check.relaunched(store, "sw", "t1") is True
    assert launch_check.relaunched(store, "sw", "t2") is False
    launch_check.clear_relaunched(store, "sw", "t1")
    assert launch_check.relaunched(store, "sw", "t1") is False


def test_told_names_every_field_and_the_outcome():
    found = {"joined": {}, "base": {}, "overlay": {}}
    assert launch_check.told(found, "spent") == (
        "The master failed its launch check within a minute on joining the ledger and holding its seat, "
        "its role on the package base role, every overlay its profile declares. "
        "Its one automatic relaunch is spent; operator action is required."
    )
    assert launch_check.told({"name": {}}, "relaunch").endswith("on its name. It is being retired and relaunched once.")
    assert launch_check.told({"settings": {}}, "report").endswith(
        "on its hooks, model and effort. This is reported only; the master keeps running."
    )
    assert "its profile" in launch_check.told({"profile": {}}, "report")


def test_findings_carry_the_field_evidence_and_threshold(store, launched):
    from scripts.swarm.health.findings import Finding

    agent, _, _ = launched
    launch_check.record(store, "sw", agent, {"name": {"expected": "a", "actual": "b"}}, LAUNCH, 0)
    assert launch_check.findings(store, "sw") == [
        Finding(
            "launch check",
            f"{agent.name}/name",
            f"{agent.name} failed its launch check on name",
            ("expected a; observed b",),
            "a launch must pass within sixty seconds",
            1,
        )
    ]


@pytest.mark.parametrize("report_only", [frozenset({"overlay"}), frozenset()])
def test_only_enforced_misses_raise_findings(store, launched, monkeypatch, report_only):
    agent, _, _ = launched
    monkeypatch.setattr(launch_check, "REPORT_ONLY", report_only)
    found = {
        "overlay": {"expected": "package:engineer", "actual": "engineer"},
        "name": {"expected": "a", "actual": "b"},
    }
    launch_check.record(store, "sw", agent, found, LAUNCH, 0)
    expected = [f"launch-check/{agent.name}/name"]
    if not report_only:
        expected.insert(0, f"launch-check/{agent.name}/overlay")
    assert [f.id for f in launch_check.findings(store, "sw")] == expected
    assert launch_check.report(store, "sw", "t1")["misses"] == found


def test_a_pending_check_for_a_gone_agent_is_forgotten(store, monkeypatch):
    ledger, runtime = checked(store, monkeypatch)
    store.update("sw", max_eng=0)
    launch_check.begin(store, "sw", AgentRecord("gone", "eng", "t9"), LAUNCH)
    tick("sw", store, ledger, runtime, LAUNCH)
    assert "gone" not in launch_check.pending(store, "sw")


def test_a_waiting_launch_does_not_stop_the_next_from_being_judged(store, monkeypatch):
    ledger, runtime = checked(store, monkeypatch)
    store.update("sw", max_eng=0)
    code = store.ensure_code("sw").code
    early = AgentRecord(f"engineer@{code}-0007", "eng", "t1", seat="eng-1@sw", started_at=LAUNCH + 50_000)
    late = AgentRecord(f"engineer@{code}-0008", "eng", "t2", seat="eng-2@sw", started_at=LAUNCH)
    for agent in (early, late):
        store.put_agent("sw", agent)
        launch_check.begin(store, "sw", agent, agent.started_at)
    actions = tick("sw", store, ledger, runtime, LAUNCH + launch_check.DEADLINE_MS)
    assert early.name in launch_check.pending(store, "sw")
    assert any(a.startswith(f"retired {late.name} after its launch check failed on joined") for a in actions)


def test_a_pass_after_a_relaunch_clears_the_mark_and_reports_its_timing(store, monkeypatch):
    ledger, runtime = checked(store, monkeypatch)
    launch_check.mark_relaunched(store, "sw", "t1")
    tick("sw", store, ledger, runtime, LAUNCH)
    joined(ledger, runtime, LAUNCH + 9_000)
    tick("sw", store, ledger, runtime, LAUNCH + 20_000)
    assert launch_check.relaunched(store, "sw", "t1") is False
    found = launch_check.report(store, "sw", "t1")
    assert (found["at"], found["elapsed_ms"], found["state"]) == (LAUNCH + 20_000, 9_000, "passed")


def test_a_relaunch_saves_the_assignment_and_retries_a_starting_master(store, monkeypatch):
    ledger, runtime = checked(store, monkeypatch)
    store.update("sw", max_eng=0)
    runtime.reported = lambda agent: False
    tick("sw", store, ledger, runtime, LAUNCH)
    (first, _) = runtime.masters[0]
    assert master_start.read(store, "sw")["name"] == first
    tick("sw", store, ledger, runtime, LAUNCH + launch_check.DEADLINE_MS)
    second = runtime.masters[1][0]
    assert master_start.read(store, "sw")["name"] == second
    assert master_start.read(store, "sw")["attempt"] == 2
    assert runtime.masters[1][1]["id"] == MASTER
    assert launch_check.relaunched(store, "sw", MASTER) is True


def test_a_relaunch_without_a_recorded_profile_takes_the_task_profile(store, monkeypatch):
    ledger, runtime = checked(store, monkeypatch, profile="", task_profile="frontend")
    tick("sw", store, ledger, runtime, LAUNCH)
    tick("sw", store, ledger, runtime, LAUNCH + launch_check.DEADLINE_MS)
    assert runtime.tasks[1]["launch_assignment"]["profile"] == "frontend"


def test_a_master_relaunch_without_a_session_slot_keeps_its_retry_window(store, monkeypatch):
    ledger, runtime = checked(store, monkeypatch)
    store.update("sw", max_eng=0)
    runtime.reported = lambda agent: False
    tick("sw", store, ledger, runtime, LAUNCH)
    runtime.full = True
    tick("sw", store, ledger, runtime, LAUNCH + launch_check.DEADLINE_MS)
    pending = master_start.read(store, "sw")
    assert (pending["name"], pending["retry"], pending["at"]) == ("", True, LAUNCH + launch_check.DEADLINE_MS)
    tick("sw", store, ledger, runtime, LAUNCH + launch_check.DEADLINE_MS + 90_000)
    assert not any("either launch" in note for note in ledger.notes)
