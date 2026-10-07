import json
from dataclasses import replace

import pytest

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
    (home / ".agentihooks-render.json").write_text(json.dumps({"chain": ["base", "package:engineer", "engineer"]}))
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
    }
    doc = {"_meta": {"members": {agent.name: {"joined_at": LAUNCH + 20_000}}}}
    return agent, facts, doc


def misses(store, agent, facts, doc, bundled=True):
    return launch_check.misses(store, "sw", agent, facts, doc, bundled)


def test_a_clean_launch_passes(store, launched):
    agent, facts, doc = launched
    assert misses(store, agent, facts, doc) == {}


def test_not_joined_is_named(store, launched):
    agent, facts, _ = launched
    assert list(misses(store, agent, facts, {"_meta": {"members": {}}})) == ["joined"]


def test_joined_after_the_deadline_is_named(store, launched):
    agent, facts, doc = launched
    doc["_meta"]["members"][agent.name]["joined_at"] = LAUNCH + launch_check.DEADLINE_MS + 1
    assert list(misses(store, agent, facts, doc)) == ["joined"]


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
    assert list(misses(store, agent, {"process": False}, doc)) == ["profile", "settings", "overlay"]


@pytest.mark.parametrize(
    "chain",
    [[], ["base", "engineer"], ["package:engineer", "engineer", "extra"], ["package:engineer"]],
)
def test_overlay_not_on_its_package_base_role_is_named(store, launched, chain):
    agent, facts, doc = launched
    assert list(misses(store, agent, {**facts, "chain": chain}, doc)) == ["overlay"]


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


def test_chain_reads_the_rendered_stamp(tmp_path):
    assert launch_check.chain(tmp_path) == []
    (tmp_path / ".agentihooks-render.json").write_text(json.dumps({"chain": ["a", "b"]}))
    assert launch_check.chain(tmp_path) == ["a", "b"]
    (tmp_path / ".agentihooks-render.json").write_text("{")
    assert launch_check.chain(tmp_path) == []


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


def checked(store, monkeypatch):
    monkeypatch.setattr(launch_check, "bundled", lambda profile: False)
    ledger, runtime = JoiningLedger([{"id": "t1", "profile": "engineer"}]), CheckedRuntime()
    runtime.spawn_profile = "engineer"
    original = runtime.spawn

    def spawn(config, lane, name, task, spawns=None):
        placed = original(config, lane, name, task, spawns)
        return replace(placed, profile="engineer")

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


def test_tick_waits_until_the_deadline_before_failing(store, monkeypatch):
    ledger, runtime = checked(store, monkeypatch)
    tick("sw", store, ledger, runtime, LAUNCH)
    actions = tick("sw", store, ledger, runtime, LAUNCH + 30_000)
    assert not any("launch check" in a for a in actions)
    assert list(launch_check.pending(store, "sw"))


def test_tick_retires_and_relaunches_a_failed_launch_once(store, monkeypatch):
    ledger, runtime = checked(store, monkeypatch)
    runtime.profile = "anton"
    tick("sw", store, ledger, runtime, LAUNCH)
    first = runtime.spawned[0][1]
    joined(ledger, runtime, LAUNCH + 1)
    actions = tick("sw", store, ledger, runtime, LAUNCH + launch_check.DEADLINE_MS)
    assert any(a.startswith(f"retired {first} after its launch check failed on profile") for a in actions)
    assert first in runtime.killed
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


def test_take_master_begins_a_check_that_never_relaunches(store):
    agent = AgentRecord(f"master@{store.ensure_code('sw').code}-0001", MASTER, MASTER, seat="master@sw")
    launch_check.begin(store, "sw", agent, LAUNCH, relaunch=False)
    assert launch_check.pending(store, "sw") == {agent.name: {"task": MASTER, "at": LAUNCH, "relaunch": False}}
