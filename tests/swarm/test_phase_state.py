import copy
import json
from collections.abc import Iterator
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


def enhancement(phase: str, state: str = "open") -> dict[str, str]:
    return {"id": f"enhance-{phase}", "phase": phase, "kind": "plan", "lane": "plan", "state": state, "enhances": "t"}


@pytest.mark.feature("swarm-phase-admits-enhancement")
@pytest.mark.parametrize(("phase", "admitted"), [("build", True), ("slice", True), ("wait", False), ("done", False)])
def test_an_enhancement_task_is_admitted_while_its_phase_plans_or_builds(phase: str, admitted: bool) -> None:
    """A plan kind task carrying enhances is admitted while its phase is planning or building, so the planner that
    enhances a thin task in a building phase can claim it; a waiting or finished phase still holds it."""
    phases = [
        {"id": "build"},
        {"id": "wait", "depends_on": ["build"]},
        {"id": "done", "done": True},
        {"id": "slice", "planning": "auto"},
    ]
    task = enhancement(phase)
    assert phase_state.admits(task, doc(phases, [task, plan("slice")])) is admitted


@pytest.mark.feature("swarm-phase-admits-enhancement")
def test_an_enhancement_task_is_never_taken_for_its_phase_plan_task() -> None:
    """An enhancement plan task in an automatic phase leaves the phase lifecycle to the phase's own plan task."""
    phases = [{"id": "slice", "planning": "auto"}]
    assert phase_state.lifecycle(phases[0], doc(phases, [enhancement("slice", "done")])) == "to_plan"
    assert phase_state.lifecycle(phases[0], doc(phases, [enhancement("slice", "done"), plan("slice")])) == "planning"


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


class CountedRows(list[dict[str, object]]):
    def __init__(self, rows: list[dict[str, object]], counts: dict[str, int], name: str) -> None:
        super().__init__(rows)
        self.counts, self.name = counts, name

    def __iter__(self) -> Iterator[dict[str, object]]:
        self.counts[self.name] += 1
        return super().__iter__()


def counted_status_doc(count: int) -> tuple[dict[str, object], dict[str, int]]:
    counts = {"phases": 0, "tasks": 0}
    phases = [
        {"id": f"p{i}", "planning": "auto", **({"depends_on": [f"p{i - 1}"]} if i % 2 else {})} for i in range(count)
    ]
    tasks = [plan(f"p{i}") for i in range(count)]
    return {"phases": CountedRows(phases, counts, "phases"), "tasks": CountedRows(tasks, counts, "tasks")}, counts


@pytest.mark.feature("swarm-phase-status-linear")
def test_status_scans_phases_and_tasks_a_fixed_number_of_times_whatever_the_phase_count() -> None:
    small, small_counts = counted_status_doc(3)
    large, large_counts = counted_status_doc(30)

    phase_state.status(small)
    phase_state.status(large)

    assert large_counts == small_counts
    assert large_counts["tasks"] <= 1


@pytest.mark.feature("swarm-phase-status-linear")
def test_status_reports_each_phase_s_lifecycle_and_label_exactly() -> None:
    phases = [
        {"id": "gone-scope", "out_of_scope": True, "done": True},
        {"id": "finished", "title": "Finished", "done": True},
        {"id": "blocked", "depends_on": ["gone", "finished", "open-dep", "gone"], "planning": "auto"},
        {"id": "open-dep", "title": "Open dependency"},
        {"id": "untitled", "title": ""},
        {"id": "waits-untitled", "depends_on": ["untitled"]},
        {"id": "manual"},
        {"id": "unplanned", "planning": "auto"},
        {"id": "planning", "planning": "auto"},
        {"id": "reviewing", "planning": "auto"},
        {"id": "approved", "planning": "auto", "review": {"state": "approved"}},
        {"id": "twice", "planning": "auto"},
        {"id": "twice", "done": True},
    ]
    tasks = [
        {"id": "loose"},
        {"id": "enhancer", "phase": "planning", "kind": "plan", "enhances": "t1", "state": "done"},
        plan("planning"),
        plan("planning", state="done"),
        plan("reviewing", state="done"),
        plan("approved", state="done"),
    ]
    data = doc(phases, tasks)
    before = copy.deepcopy(data)

    found = phase_state.status(data)

    expected = {p["id"]: {"state": phase_state.lifecycle(p, data), "label": phase_state.label(p, data)} for p in phases}
    assert found == expected
    assert found["blocked"] == {"state": "waiting", "label": "Waits for phase gone, Open dependency, gone"}
    assert found["waits-untitled"] == {"state": "waiting", "label": "Waits for phase untitled"}
    assert [found[key]["state"] for key in ("gone-scope", "finished", "manual", "unplanned", "planning")] == [
        "out_of_scope",
        "done",
        "building",
        "to_plan",
        "planning",
    ]
    assert [found[key]["state"] for key in ("reviewing", "approved", "twice")] == ["in_review", "building", "done"]
    assert list(found) == list(dict.fromkeys(p["id"] for p in phases))
    assert data == before
    assert phase_state.status({}) == {}
    assert phase_state.status({"tasks": []}) == {}
    assert phase_state.status({"phases": [{"id": "m"}, {"id": "w", "depends_on": ["m"], "planning": "auto"}]}) == {
        "m": {"state": "building", "label": phase_state.LABELS["building"]},
        "w": {"state": "waiting", "label": "Waits for phase m"},
    }


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
