import pytest

from scripts.swarm import tick as tick_module
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeRuntime, tasks
from tests.swarm.test_tick import store as store

pytestmark = [pytest.mark.unit]


class OrderingRuntime(FakeRuntime):
    def __init__(self, order):
        super().__init__()
        self.order = order

    def spawn(self, config, lane, name, task):
        if lane != "master":
            self.order.append("spawn")
        return super().spawn(config, lane, name, task)


def test_spawn_is_attempted_before_reap_and_exits_sweep(store, monkeypatch):
    order = []
    monkeypatch.setattr(tick_module, "_reap", lambda *a, **k: order.append("reap") or [])
    monkeypatch.setattr(tick_module.exits, "sweep", lambda *a, **k: order.append("exits.sweep") or [])
    ledger, runtime = tasks(("t1", "eng")), OrderingRuntime(order)
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert order == ["spawn", "reap", "exits.sweep"]
    assert len(runtime.spawned) == 1
    assert runtime.spawned[0][0] == "eng"
    assert runtime.spawned[0][2] == "t1"
