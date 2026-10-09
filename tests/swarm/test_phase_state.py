import json
from pathlib import Path

import pytest

from scripts.swarm import phase_state
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeLedger, FakeRuntime

pytestmark = pytest.mark.xdist_group("fakeredis")


def doc(phases, tasks=()):
    return {"phases": list(phases), "tasks": list(tasks)}


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


def spawned(runtime):
    return [task for _, _, task in runtime.spawned]


def test_a_task_in_a_waiting_phase_is_claimed_on_the_tick_after_its_phase_is_done(store):
    ledger = FakeLedger([{"id": "a", "phase": "p1", "state": "done"}, {"id": "b", "phase": "p2"}])
    ledger.phases = [{"id": "p1"}, {"id": "p2", "depends_on": ["p1"]}]
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned(runtime) == [] and store.config("sw").state == "drained"
    ledger.phases[0]["done"] = True
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert spawned(runtime) == ["b"]


def test_an_auto_phase_holds_its_tasks_until_the_plan_is_approved(store):
    ledger = FakeLedger([{**plan("p1", "done"), "lane": "eng"}, {"id": "b", "phase": "p1"}])
    ledger.phases = [{"id": "p1", "planning": "auto", "review": {"state": "sent_back"}}]
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned(runtime) == []
    ledger.phases[0]["review"] = {"state": "approved"}
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert spawned(runtime) == ["b"]


def test_the_plan_task_is_claimed_only_while_its_phase_is_planning(store):
    from pathlib import Path

    ledger = FakeLedger([{**plan("p1"), "lane": "plan"}, {"id": "b", "phase": "p1"}])
    ledger.phases = [
        {"id": "p1", "title": "Build", "planning": "auto", "depends_on": ["p0"]},
        {"id": "p0", "title": "Base"},
    ]
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned(runtime) == []
    ledger.phases[1]["done"] = True
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert spawned(runtime) == ["plan-p1"]
    steering = (Path(ledger.rows["plan-p1"]["workspace"]) / "steering.md").read_text()
    assert "Project intent\nProject intent" in steering
    assert "Dependency tasks for Base" in steering


class OwnLedger(FakeLedger):
    def state(self, slug):
        return super().state(slug) if slug == "sw" else {"tasks": [], "phases": []}


def test_tasks_with_no_phase_or_an_unknown_phase_claim_as_today(store):
    ledger = OwnLedger([{"id": "a"}, {"id": "b", "phase": "missing"}])
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned(runtime) == ["a", "b"]


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
    from scripts.swarm import phases

    class Ledger:
        ticked = []

        def set_phase(self, slug, phase_id, done, status):
            self.ticked.append(phase_id)

        def hierarchy(self, slug):
            return [node("phases/p1", 0), node("phases/p2", 0), node("tasks/plan-p2", 1)]

    data = doc([{"id": "p1", "planning": "auto"}, {"id": "p2", "planning": "auto"}], [plan("p2")])
    assert [phase_state.lifecycle(p, data) for p in data["phases"]] == ["to_plan", "planning"]
    assert phases.phase_pass(None, store, "sw", data, Ledger()) == [] and Ledger.ticked == []


def node(address, depth):
    return {"node": address, "kind": address.split("/")[0][:-1], "depth": depth}


def test_phase_pass_reads_each_phase_s_tasks_from_the_ledger_hierarchy(store):
    from scripts.swarm import phases

    class Inbox:
        sent = []

        def send(self, sender, to, text, fyi=False):
            self.sent.append(text)

    class Ledger:
        ticked = []

        def set_phase(self, slug, phase_id, done, status):
            self.ticked.append((phase_id, done))

        def hierarchy(self, slug):
            return [
                node("plans/a", 0),
                node("phases/p1", 1),
                node("slices/s1", 2),
                node("tasks/t1", 3),
                node("tasks/t2", 2),
                node("phases/p2", 1),
                node("tasks/t3", 2),
                node("tasks/t4", 0),
            ]

    data = doc(
        [{"id": "p1", "title": "Build"}, {"id": "p2", "title": "Ship", "done": True}],
        [
            {"id": "t1", "phase": "p1", "state": "done"},
            {"id": "t2", "phase": "p1", "state": "done"},
            {"id": "t3", "phase": "p2", "state": "open"},
            {"id": "t4", "phase": "p1", "state": "open"},
        ],
    )
    assert phases.phase_pass(Inbox(), store, "sw", data, Ledger()) == ["phase p1 ticked", "phase p2 reopened"]
    assert Ledger.ticked == [("p1", True), ("p2", False)]
    assert Inbox.sent[0].endswith("all 2 tasks closed.") and Inbox.sent[1].endswith("reopened for t3.")


def test_the_ledger_client_reads_the_hierarchy_resource(monkeypatch):
    from scripts.swarm import ledger_client

    read = []
    monkeypatch.setattr(
        ledger_client.LedgerClient, "_resource", lambda self, slug, path, collection=False: read.append(path) or []
    )
    assert ledger_client.LedgerClient().hierarchy("sw") == [] and read == ["hierarchy"]
