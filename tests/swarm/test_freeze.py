import pytest

from scripts.doctor import loop, priming
from scripts.swarm import capacity, freeze
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm.tick import tick
from scripts.swarm_ledger.repository import hierarchy
from tests.swarm.test_tick import FakeLedger, FakeRuntime, workers

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

PLANS = [{"id": "a", "title": "Swarm v2"}, {"id": "b", "title": "Ledger polish"}]
PHASES = [
    {"id": "p1", "title": "Build", "plan": "plans/a"},
    {"id": "p2", "title": "Ship", "plan": "plans/b"},
]
SLICES = [{"id": "a.first", "phase": "phases/p1"}]


def record(target, verb="freeze"):
    return {"id": f"{verb}-{target}", "verb": verb, "target": target, "by": "operator", "at": 1, "reason": ""}


def doc(*freezes, tasks=()):
    return {"plans": PLANS, "phases": PHASES, "slices": SLICES, "tasks": list(tasks), "freezes": list(freezes)}


def task(task_id="t1", **fields):
    return {"id": task_id, "lane": "eng", "state": "open", "phase": "p1", **fields}


def held(row, *freezes, fix_phase=""):
    found = doc(*freezes, tasks=[row])
    return freeze.held(row, found, hierarchy.project(found)[0], fix_phase)


@pytest.mark.parametrize(
    "target",
    ["plans/a", "phases/p1", "slices/a.first", "tasks/t1", "lane:eng", "kind:troubleshoot"],
)
def test_a_task_under_a_frozen_node_or_matching_a_frozen_selector_is_held(target):
    row = task(slice="slices/a.first", kind="troubleshoot")
    assert held(row, record(target))


@pytest.mark.parametrize("target", ["plans/b", "phases/p2", "tasks/t2", "lane:ci", "kind:code"])
def test_a_freeze_elsewhere_leaves_the_task_free(target):
    assert not held(task(slice="slices/a.first", kind="troubleshoot"), record(target))


def test_a_task_without_a_kind_matches_the_code_kind():
    assert held(task(), record("kind:code"))


def test_no_freezes_hold_nothing():
    assert not held(task())
    assert not freeze.held(task(), {"tasks": []}, {}, "")


def test_a_direct_freeze_holds_an_urgent_task_and_a_doctor_fix():
    assert held(task(rank="urgent"), record("phases/p1"))
    assert held(task(phase=loop.FIX_PHASE), record("lane:eng"), fix_phase=loop.FIX_PHASE)


@pytest.mark.parametrize("target", ["plans/b", "phases/p2", "lane:ci", "kind:research"])
def test_a_focus_elsewhere_holds_a_normal_task(target):
    assert held(task(), record(target, "focus"))


@pytest.mark.parametrize("target", ["plans/a", "phases/p1", "slices/a.first", "tasks/t1", "lane:eng", "kind:code"])
def test_a_task_inside_any_focus_is_free(target):
    row = task(slice="slices/a.first")
    assert not held(row, record("plans/b", "focus"), record(target, "focus"))


def test_an_urgent_task_and_a_doctor_fix_pass_a_focus():
    assert not held(task(rank="urgent"), record("plans/b", "focus"))
    assert not held(task(phase=loop.FIX_PHASE), record("lane:ci", "focus"), fix_phase=loop.FIX_PHASE)
    assert held(task(phase=loop.FIX_PHASE), record("lane:ci", "focus"))
    assert held(task(phase=loop.FIX_PHASE), record("lane:ci", "focus"), fix_phase="p7")
    assert held(task(rank="high"), record("plans/b", "focus"))


@pytest.mark.parametrize("state", ["claimed", "pr", "blocked", "done"])
def test_claimed_pull_request_and_blocked_work_is_never_held(state):
    assert not held(task(state=state), record("tasks/t1"))
    assert not held(task(state=state), record("plans/b", "focus"))


def test_a_cycle_in_the_parent_links_ends_the_walk():
    row = task()
    graph = {
        "tasks/t1": ("task", "phases/p1", 0),
        "phases/p1": ("phase", "plans/a", 0),
        "plans/a": ("plan", "phases/p1", 0),
    }
    assert freeze.ancestry(row, graph) == ["tasks/t1", "phases/p1", "plans/a"]
    assert freeze.held(row, doc(record("plans/a")), graph, "")
    assert not freeze.held(row, doc(record("plans/b")), graph, "")


def test_the_walk_climbs_to_the_root_and_stops_at_the_chain_bound():
    found = doc(tasks=[task(slice="slices/a.first")])
    assert freeze.ancestry(found["tasks"][0], hierarchy.project(found)[0]) == [
        "tasks/t1",
        "slices/a.first",
        "phases/p1",
        "plans/a",
    ]
    assert freeze.ancestry({"id": "t1"}, {}) == ["tasks/t1"]
    graph = {f"phases/n{i}": ("phase", f"phases/n{i + 1}", 0) for i in range(1, 80)}
    chain = freeze.ancestry(task(phase="n1"), graph)
    assert len(chain) == hierarchy.CHAIN
    assert chain[-1] == f"phases/n{hierarchy.CHAIN - 1}"


def test_names_say_each_freeze_in_plain_words():
    found = doc(
        record("plans/a"),
        record("phases/p2", "focus"),
        record("slices/a.first"),
        record("tasks/t9"),
        record("tasks/t8", "focus"),
        record("lane:ci", "focus"),
        record("kind:research"),
        tasks=[task("t9", title="Wire the page")],
    )
    assert freeze.names(found) == [
        "the freeze on plan Swarm v2",
        "the focus on phase Ship",
        "the freeze on slice a.first",
        "the freeze on task Wire the page",
        "the focus on task t8",
        "the focus on the ci lane",
        "the freeze on research tasks",
    ]


def test_the_drain_notice_counts_held_work_and_names_only_the_freezes_holding_it():
    one = doc(record("plans/a"), record("plans/b"), tasks=[task()])
    assert freeze.notice(one, one["tasks"], "") == (
        "The swarm has no task it may start: 1 open task is held by the freeze on plan Swarm v2"
    )
    rows = [task(), task("t2", phase="p2"), task("t3", out_of_scope=True), task("t4", state="claimed")]
    two = doc(record("plans/a"), record("plans/b"), record("lane:ci", "focus"), tasks=rows)
    assert freeze.notice(two, rows, "") == (
        "The swarm has no task it may start: 2 open tasks are held by the freeze on plan Swarm v2, "
        "the freeze on plan Ledger polish and the focus on the ci lane"
    )
    assert freeze.notice(doc(record("plans/b"), tasks=[task()]), [task()], "") == freeze.DRAINED
    inside = doc(record("plans/a", "focus"), record("phases/p1"), tasks=[task()])
    assert freeze.notice(inside, inside["tasks"], "") == (
        "The swarm has no task it may start: 1 open task is held by the freeze on phase Build"
    )


class FrozenLedger(FakeLedger):
    def __init__(self, tasks, *freezes):
        super().__init__(tasks)
        self.phases, self.freezes = [dict(p) for p in PHASES], list(freezes)

    def state(self, slug):
        return {**super().state(slug), "plans": PLANS, "slices": SLICES, "freezes": self.freezes}


@pytest.fixture
def store():
    import fakeredis

    s = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    s.create(SwarmConfig("sw", "/repo", max_eng=2, max_ci=1))
    return s


def spawned(runtime):
    return [task_id for _, _, task_id in runtime.spawned]


def test_a_frozen_phase_keeps_its_urgent_task_open(store):
    ledger = FrozenLedger(
        [{"id": "t1", "phase": "p1", "rank": "urgent"}, {"id": "t2", "phase": "p2"}], record("phases/p1")
    )
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned(runtime) == ["t2"]
    assert ledger.rows["t1"]["state"] == "open"


def test_a_focused_plan_lets_an_urgent_task_outside_it_be_claimed_and_holds_a_normal_one(store):
    ledger = FrozenLedger(
        [{"id": "normal", "phase": "p2"}, {"id": "urgent", "phase": "p2", "rank": "urgent"}],
        record("plans/a", "focus"),
    )
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert spawned(runtime) == ["urgent"]
    assert ledger.rows["normal"]["state"] == "open"


def test_a_claimed_task_under_a_new_freeze_keeps_its_agent(store):
    ledger, runtime = FrozenLedger([{"id": "t1", "phase": "p1"}]), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    agent = workers(store)[0]
    ledger.freezes = [record("phases/p1")]
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert runtime.killed == []
    assert [a.name for a in workers(store)] == [agent.name]
    assert ledger.rows["t1"]["claimed_by"] == agent.name
    ledger.rows["t1"]["state"] = "pr"
    tick("sw", store, ledger, runtime, now_ms=3_000)
    assert runtime.killed == [] and ledger.rows["t1"]["state"] == "pr"
    ledger.rows["t1"]["state"] = "done"
    tick("sw", store, ledger, runtime, now_ms=4_000)
    assert spawned(runtime) == ["t1"] and ledger.rows["t1"]["state"] == "done"


def test_a_doctor_swarm_claims_its_fix_task_under_a_focus(store):
    store.update("sw", template=priming.TEMPLATE)
    ledger = FrozenLedger(
        [{"id": "fix", "phase": loop.FIX_PHASE}, {"id": "watch", "phase": "p1"}], record("lane:ci", "focus")
    )
    _, ready = capacity.ready_work("sw", store, ledger.state("sw"))
    assert [t["id"] for t in ready["eng"]] == ["fix"]


def test_autoscale_demand_counts_no_held_task(store):
    ledger = FrozenLedger([{"id": "t1", "phase": "p1"}, {"id": "t2", "phase": "p2"}], record("plans/a"))
    _, ready = capacity.ready_work("sw", store, ledger.state("sw"))
    assert [t["id"] for t in ready["eng"]] == ["t2"]


def test_only_held_work_drains_with_a_notice_naming_the_freezes_and_unfreezing_runs_again(store):
    ledger = FrozenLedger([{"id": "t1", "phase": "p1"}], record("plans/a"), record("lane:ci", "focus"))
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert store.config("sw").state == "drained"
    assert runtime.spawned == []
    assert ledger.notes == [
        "The swarm has no task it may start: 1 open task is held by the freeze on plan Swarm v2 "
        "and the focus on the ci lane"
    ]
    ledger.freezes = []
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert store.config("sw").state == "running"
    assert spawned(runtime) == ["t1"]


def test_a_doctor_drain_counts_no_fix_task_as_held(store):
    store.update("sw", template=priming.TEMPLATE)
    ledger = FrozenLedger(
        [
            {"id": "cause", "phase": loop.FIX_PHASE, "state": "blocked"},
            {"id": "fix", "phase": loop.FIX_PHASE, "depends_on": ["cause"]},
        ],
        record("lane:ci", "focus"),
    )
    tick("sw", store, ledger, FakeRuntime(), now_ms=1_000)
    assert store.config("sw").state == "drained"
    assert ledger.notes == ["The swarm has no task left to start, one blocked task waits for you"]


def test_a_drain_with_nothing_held_keeps_the_plain_notice(store):
    ledger = FrozenLedger([{"id": "t1", "phase": "p1", "state": "done"}], record("plans/b"))
    tick("sw", store, ledger, FakeRuntime(), now_ms=1_000)
    assert ledger.notes == ["The swarm has no task left to start"]
