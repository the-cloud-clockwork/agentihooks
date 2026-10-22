from scripts.handoff import transfers
from scripts.swarm.store import MASTER, AgentRecord, SwarmError
from scripts.swarm.tick import tick
from scripts.swarm_v2.admission import ADMITTED, RELEASED, PendingAdmission, Policy, Resources, Template
from tests.swarm.test_tick import FakeRuntime, store, tasks  # noqa: F401

ROOM = "no hive for eng has room: h1 max agents: 1 of 1 agents on the hive, fits 0 more"
GENERAL = Template("general", Resources(16384, 4000))


class FillingRuntime(FakeRuntime):
    def placement_refusal(self, lane, task):
        return ROOM if self.spawned else ""

    def spawn(self, config, lane, name, task):
        if refused := self.placement_refusal(lane, task):
            raise SwarmError(refused)
        return super().spawn(config, lane, name, task)


def test_a_hive_filled_earlier_in_the_tick_leaves_the_next_task_waiting_not_failed(store):  # noqa: F811
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng")), FillingRuntime()
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert f"task t2 waits: {ROOM}" in actions
    assert not [action for action in actions if action.startswith("spawn failed")]
    assert [task for _, _, task in runtime.spawned] == ["t1"]
    assert ledger.rows["t2"]["state"] == "open"
    assert ledger.rows["t2"]["claimed_by"] == ""
    assert ledger.rows["t2"].get("claim_generation", 0) == 0
    assert [row["task"] for row in store.launches("sw")] == ["t1"]


class RacingRuntime(FakeRuntime):
    def __init__(self, saved):
        super().__init__()
        self.taken = False
        self.admission = PendingAdmission(saved, Policy(3, 3, 100, (GENERAL,), Resources(1, 1)), lambda s: 3)

    def placement_refusal(self, lane, task):
        return ROOM if self.taken and lane == "eng" else ""

    def spawn(self, config, lane, name, task):
        if lane == MASTER:
            return super().spawn(config, lane, name, task)
        self.taken = True
        raise SwarmError(ROOM)


def test_a_hive_filled_between_the_check_and_the_spawn_is_a_wait_not_a_failed_launch(store):  # noqa: F811
    ledger, runtime = tasks(("t1", "eng")), RacingRuntime(store)
    transfers.record(store, "sw", AgentRecord("old", "eng", "t1", seat="eng-1@sw"), "recycle", "Handoff", 500)
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert f"task t1 waits: {ROOM}" in actions
    assert not [action for action in actions if action.startswith("spawn failed")]
    assert ledger.rows["t1"]["state"] == "open"
    assert ledger.rows["t1"]["claimed_by"] == ""
    assert store.claimant("sw", "t1") is None
    assert [(row["task"], row["state"], row["error"]) for row in store.launches("sw")] == [("t1", "waiting", ROOM)]
    assert store.launch_failure("sw", "t1") == ""
    assert [row["successor"] for row in transfers.list_transfers(store, "sw")] == [""]
    assert runtime.admission.pending_execution_admission_total("sw") == {ADMITTED: 1, RELEASED: 1}
