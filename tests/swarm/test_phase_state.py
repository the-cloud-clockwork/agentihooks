import json
from pathlib import Path

import pytest

from scripts.swarm import phase_state
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeLedger, FakeRuntime


def doc(phases, tasks=()):
    return {"phases": list(phases), "tasks": list(tasks)}


def phase_pass(inbox, store, slug, data, ledger):
    from scripts.swarm import phases

    found = phases.phase_changes(data)
    return [item if isinstance(item, str) else phases.apply_change(inbox, store, slug, ledger, item) for item in found]


def plan(phase, state="open"):
    return {"id": f"plan-{phase}", "phase": phase, "kind": "plan", "lane": "plan", "state": state}


LIFECYCLE_CASES = json.loads((Path(__file__).parents[1] / "fixtures" / "phase_lifecycle.json").read_text())


@pytest.mark.parametrize("case", LIFECYCLE_CASES)
def test_lifecycle_row(case):
    phase = next(p for p in case["phases"] if p["id"] == case["phase"])
    assert phase_state.lifecycle(phase, doc(case["phases"], case["tasks"])) == case["state"]


@pytest.mark.parametrize(
    ("task", "admitted"),
    [
        ({"id": "t", "phase": ""}, True),
        ({"id": "t"}, True),
        ({"id": "t", "phase": "nowhere"}, True),
        ({"id": "t", "phase": "build"}, True),
        ({"id": "t", "phase": "wait"}, False),
        ({"id": "t", "phase": "done"}, False),
        ({"id": "t", "phase": "slice"}, False),
        (plan("slice"), True),
        (plan("build"), False),
        (plan("wait"), False),
    ],
)
def test_admits_only_building_tasks_and_the_planning_plan_task(task, admitted):
    phases = [
        {"id": "build"},
        {"id": "wait", "depends_on": ["build"]},
        {"id": "done", "done": True},
        {"id": "slice", "planning": "auto"},
    ]
    assert phase_state.admits(task, doc(phases, [task, plan("slice")])) is admitted


def test_a_ledger_without_a_phases_list_holds_nothing():
    old = {"tasks": [{"id": "t", "phase": "p1", "state": "open"}]}
    assert phase_state.lifecycle({"id": "p1", "depends_on": ["p0"]}, old) == "waiting"
    assert phase_state.admits(old["tasks"][0], old) is True
    assert phase_state.report(old) == []


@pytest.fixture
def store():
    import fakeredis

    s = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    s.create(SwarmConfig("sw", "/repo", max_eng=2, max_ci=0, state="running"))
    return s


def test_a_swarm_with_no_free_slot_but_a_building_task_stays_running(store):
    store.update("sw", max_eng=0)
    ledger = FakeLedger([{"id": "a", "phase": "p1"}])
    ledger.phases = [{"id": "p1"}]
    tick("sw", store, ledger, FakeRuntime(), now_ms=1_000)
    assert store.config("sw").state == "running"


def test_held_lists_each_open_task_a_phase_holds_with_its_state():
    phases = [{"id": "p1"}, {"id": "p2", "depends_on": ["p1"]}]
    tasks = [
        {"id": "a", "phase": "p1", "state": "open"},
        {"id": "b", "phase": "p2", "state": "open"},
        {"id": "c", "phase": "p2", "state": "done"},
        {"id": "d", "phase": "p2", "state": "open", "out_of_scope": True},
    ]
    assert phase_state.report(doc(phases, tasks)) == [("p1", "building", []), ("p2", "waiting", ["b"])]


def test_phase_pass_never_ticks_a_phase_to_plan_or_planning(store):

    class Ledger:
        ticked = []

        def set_phase(self, slug, phase_id, done, status):
            self.ticked.append(phase_id)

        def hierarchy(self, slug):
            raise AssertionError("the phase pass projects the pass document")

    data = doc([{"id": "p1", "planning": "auto"}, {"id": "p2", "planning": "auto"}], [plan("p2")])
    assert [phase_state.lifecycle(p, data) for p in data["phases"]] == ["to_plan", "planning"]
    assert phase_pass(None, store, "sw", data, Ledger()) == [] and Ledger.ticked == []


def test_phase_pass_reads_each_phase_s_tasks_and_states_from_the_pass_document(store):

    class Inbox:
        sent = []

        def send(self, sender, to, text, fyi=False):
            self.sent.append(text)

    class Ledger:
        ticked = []

        def set_phase(self, slug, phase_id, done, status):
            self.ticked.append((phase_id, done))

        def hierarchy(self, slug):
            raise AssertionError("the phase pass projects the pass document")

    data = doc(
        [
            {"id": "p1", "title": "Build", "plan": "plans/a"},
            {"id": "p2", "title": "Ship", "done": True, "plan": "plans/a"},
            {"id": "p3", "title": "Lone"},
            {"id": "p9", "title": "Added after the read"},
        ],
        [
            {"id": "t1", "slice": "slices/s1", "state": "done"},
            {"id": "t2", "phase": "p1", "state": "done"},
            {"id": "t5", "phase": "p1", "state": "open", "out_of_scope": True},
            {"id": "t3", "phase": "p2", "state": "open"},
            {"id": "t8", "phase": "p2"},
            {"id": "t4", "state": "open"},
            {"id": "t6", "phase": "p3", "done": True},
            {"id": "t7", "phase": "missing", "state": "open"},
        ],
    )
    data["plans"] = [{"id": "a"}]
    data["slices"] = [{"id": "s1", "phase": "phases/p1"}]
    assert phase_pass(Inbox(), store, "sw", data, Ledger()) == [
        "phase p1 ticked",
        "phase p2 reopened",
        "phase p3 ticked",
    ]
    assert Ledger.ticked == [("p1", True), ("p2", False), ("p3", True)]
    assert Inbox.sent[0].endswith("all 2 tasks closed.") and Inbox.sent[1].endswith("reopened for t3, t8.")


def test_the_ledger_client_reads_the_hierarchy_resource(monkeypatch):
    from scripts.swarm import ledger_client

    read = []
    monkeypatch.setattr(
        ledger_client.LedgerClient,
        "_resource",
        lambda self, slug, path, collection=False: read.append((path, collection)) or [],
    )
    assert ledger_client.LedgerClient().hierarchy("sw") == [] and read == [("hierarchy", True)]
