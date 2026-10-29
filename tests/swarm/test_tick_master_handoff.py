from dataclasses import replace

from scripts.swarm.store import MASTER
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeRuntime, masters, store, tasks

__all__ = ["store"]


class HandoffMidPass(FakeRuntime):
    def __init__(self, store):
        super().__init__()
        self.store, self.armed = store, False

    def observe(self, agent):
        if self.armed and agent.lane == MASTER:
            self.armed = False
            (old,) = masters(self.store)
            self.store.put_handoff("sw", MASTER, "operator wants caps at four")
            self.store.put_agent("sw", replace(old, state="finished"))
        return super().observe(agent)


def test_a_master_handoff_landing_mid_tick_still_launches_the_successor_with_the_doc(store):
    runtime = HandoffMidPass(store)
    tick("sw", store, tasks(), runtime, 1)
    runtime.armed = True
    tick("sw", store, tasks(), runtime, 2)
    assert [a.state for a in masters(store)] == ["finished"]
    actions = tick("sw", store, tasks(), runtime, 3)
    assert "retired master@a1b2c3-0001" in actions and "spawned master master@a1b2c3-0002" in actions
    assert runtime.masters[-1][1]["handoff"] == "operator wants caps at four"
    assert [a.name for a in masters(store)] == ["master@a1b2c3-0002"]


def test_a_finished_agent_is_never_written_back_to_working(store):
    tick("sw", store, tasks(), FakeRuntime(), 1)
    (old,) = masters(store)
    store.put_agent("sw", replace(old, state="finished"))
    store.put_agent("sw", replace(old, state="working", idle_ticks=2))
    assert [(a.state, a.idle_ticks) for a in masters(store)] == [("finished", 2)]
    store.execution_registry.update("sw", replace(old, state="ended"))
    assert store.execution_registry.get("sw", old.execution_id).state == "ended"
