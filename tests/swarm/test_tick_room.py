from scripts.swarm.store import SwarmError
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeRuntime, store, tasks  # noqa: F401

ROOM = "no hive for eng has room: h1 max agents: 1 of 1 agents on the hive, fits 0 more"


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
