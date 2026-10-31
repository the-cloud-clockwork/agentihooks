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

    def end_placement_pass(self):
        self.order.append("end")


def _ordered(store, monkeypatch):
    order = []
    monkeypatch.setattr(tick_module, "_reap", lambda *a, **k: order.append("reap") or [])
    monkeypatch.setattr(tick_module.exits, "sweep", lambda *a, **k: order.append("exits.sweep") or [])
    monkeypatch.setattr(tick_module.automated_gc, "run", lambda *a, **k: ["collected before the spawn"])
    runtime = OrderingRuntime(order)
    runtime.actions = tick("sw", store, tasks(("t1", "eng")), runtime, now_ms=1_000)
    return order, runtime


def test_a_pass_after_one_that_never_reached_its_spawn_spawns_before_exits_sweep_and_reap(store, monkeypatch):
    store.redis.set(store.key("sw", tick_module.SPAWN_DUE), 500)
    order, runtime = _ordered(store, monkeypatch)
    assert order[:4] == ["spawn", "end", "exits.sweep", "reap"]
    assert "collected before the spawn" in runtime.actions
    assert [(lane, task) for lane, _, task in runtime.spawned if lane != "master"] == [("eng", "t1")]
    assert not store.redis.exists(store.key("sw", tick_module.SPAWN_DUE))


def test_a_pass_after_a_completed_one_spawns_after_reap_and_clears_its_due_mark(store, monkeypatch):
    order, runtime = _ordered(store, monkeypatch)
    assert order[:4] == ["exits.sweep", "reap", "spawn", "end"]
    assert [(lane, task) for lane, _, task in runtime.spawned if lane != "master"] == [("eng", "t1")]
    assert not store.redis.exists(store.key("sw", tick_module.SPAWN_DUE))


def test_a_paused_pass_keeps_the_due_mark_for_the_next_running_pass(store, monkeypatch):
    store.redis.set(store.key("sw", tick_module.SPAWN_DUE), 500)
    store.update("sw", state="paused")
    order, runtime = _ordered(store, monkeypatch)
    assert "spawn" not in order
    assert store.redis.get(store.key("sw", tick_module.SPAWN_DUE)) == "500"


def test_a_pass_stopped_before_its_spawn_leaves_the_next_pass_due(store, monkeypatch):
    def stopped(*a, **k):
        raise SystemExit("stopped by the service")

    monkeypatch.setattr(tick_module, "_reap", stopped)
    with pytest.raises(SystemExit):
        tick("sw", store, tasks(("t1", "eng")), FakeRuntime(), now_ms=1_000)
    assert store.redis.get(store.key("sw", tick_module.SPAWN_DUE)) == "1000"
