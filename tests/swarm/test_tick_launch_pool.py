import threading
import time

import pytest

from scripts.swarm import capacity, runtime
from scripts.swarm import tick as tick_module
from scripts.swarm.tick import SPAWN_POOL, SpawnError, tick
from tests.swarm.test_host_gate import _decide, _held, _ledger, _workers
from tests.swarm.test_tick import FakeRuntime, tasks
from tests.swarm.test_tick import store as store
from tests.test_profile_binding_transfers import launching as launching


class PooledRuntime(FakeRuntime):
    def __init__(self, room, hold=0.0, barrier=None):
        super().__init__()
        self.room, self.hold, self.barrier = room, hold, barrier
        self.lock, self.running, self.peak = threading.Lock(), 0, 0

    def launch_room(self, config):
        return self.room

    def spawn(self, config, lane, name, task):
        if lane == "master":
            return super().spawn(config, lane, name, task)
        with self.lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
        try:
            if self.barrier is not None:
                self.barrier.wait()
            time.sleep(self.hold)
            with self.lock:
                return super().spawn(config, lane, name, task)
        finally:
            with self.lock:
                self.running -= 1


def _eight(store):
    store.update("sw", max_eng=8)
    return tasks(*((f"t{n}", "eng") for n in range(1, 9)))


def test_eight_launches_of_one_pass_run_at_once(store):
    ledger, pooled = _eight(store), PooledRuntime(8, barrier=threading.Barrier(8, timeout=10))
    actions = tick("sw", store, ledger, pooled, now_ms=1_000)
    assert sorted(task for _, _, task in pooled.spawned) == [f"t{n}" for n in range(1, 9)]
    assert pooled.peak == 8
    assert all(ledger.rows[f"t{n}"]["state"] == "claimed" for n in range(1, 9))
    assert sum(action.startswith("spawned engineer@") for action in actions) == 8


@pytest.mark.wall_clock
def test_concurrent_launches_finish_in_the_time_of_the_slowest(store):
    ledger, pooled = _eight(store), PooledRuntime(8, hold=0.5)
    started = time.monotonic()
    tick("sw", store, ledger, pooled, now_ms=1_000)
    assert len(pooled.spawned) == 8
    assert time.monotonic() - started < 3.0


@pytest.mark.parametrize("room,peak", [(3, 3), (50, SPAWN_POOL), (0, 1)])
def test_the_pool_never_runs_more_launches_than_the_runtime_room_or_the_pool(store, room, peak):
    assert SPAWN_POOL == 8
    store.update("sw", max_eng=10)
    ledger = tasks(*((f"t{n}", "eng") for n in range(1, 11)))
    pooled = PooledRuntime(room, hold=0.05)
    tick("sw", store, ledger, pooled, now_ms=1_000)
    assert pooled.peak == peak
    assert len(pooled.spawned) == 10


def test_a_runtime_without_a_launch_room_launches_one_at_a_time(store):
    ledger, plain = _eight(store), FakeRuntime()
    running, peak, spawn = [0], [0], plain.spawn

    def counted(config, lane, name, task):
        running[0] += 1
        peak[0] = max(peak[0], running[0])
        time.sleep(0.01)
        running[0] -= 1
        return spawn(config, lane, name, task)

    plain.spawn = counted
    tick("sw", store, ledger, plain, now_ms=1_000)
    assert peak[0] == 1
    assert len(plain.spawned) == 8


def test_launches_in_flight_count_against_the_host_room(store):
    ledger, pooled = _ledger(), PooledRuntime(8)
    _decide(store, 2)
    actions = tick("sw", store, ledger, pooled, now_ms=1_000)
    assert _workers(pooled) == ["t1"]
    assert _held(2, 2) in actions


def test_a_failed_launch_in_the_pool_releases_its_task_and_the_rest_start(store):
    ledger, pooled = _eight(store), PooledRuntime(8)
    spawn = pooled.spawn

    def failing(config, lane, name, task):
        if task["id"] == "t3":
            raise SpawnError("profile canary did not validate")
        return spawn(config, lane, name, task)

    pooled.spawn = failing
    actions = tick("sw", store, ledger, pooled, now_ms=1_000)
    assert sorted(task for _, _, task in pooled.spawned) == [f"t{n}" for n in range(1, 9) if n != 3]
    assert any(action.startswith("spawn failed for t3") for action in actions)
    assert store.claimant("sw", "t3") is None


def test_a_pool_whose_every_launch_failed_spares_the_spawn_budget(store, monkeypatch):
    from types import SimpleNamespace

    store.update("sw", max_eng=3)
    ledger, pooled = tasks(("t1", "eng"), ("t2", "eng"), ("t3", "eng")), PooledRuntime(2)
    clock = [0.0]
    monkeypatch.setattr(tick_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    spawn = pooled.spawn

    def launch(config, lane, name, task):
        if task["id"] in ("t1", "t2"):
            clock[0] = 60.0
            raise SpawnError("profile canary did not validate")
        return spawn(config, lane, name, task)

    pooled.spawn = launch
    actions = tick("sw", store, ledger, pooled, now_ms=1_000)
    assert [task for _, _, task in pooled.spawned] == ["t3"]
    assert not any("launch budget" in action for action in actions)


def test_a_started_pool_spends_the_spawn_budget_for_the_next_one(store, monkeypatch):
    from types import SimpleNamespace

    store.update("sw", max_eng=3)
    ledger, pooled = tasks(("t1", "eng"), ("t2", "eng"), ("t3", "eng")), PooledRuntime(2)
    clock = [0.0]
    monkeypatch.setattr(tick_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    spawn = pooled.spawn

    def launch(config, lane, name, task):
        if lane != "master":
            clock[0] = 25.0
        return spawn(config, lane, name, task)

    pooled.spawn = launch
    actions = tick("sw", store, ledger, pooled, now_ms=1_000)
    assert sorted(task for _, _, task in pooled.spawned) == ["t1", "t2"]
    assert "spent this pass's 20 second launch budget, t3 waits a pass" in actions


def _quota_launch(launching, fail=False):
    engine, config, task, saved, calls = launching
    saved.update(harness="claude")
    task["handoff_envelope"]["reason"] = "quota"
    engine._quota_accounts = [capacity.Account("claude", "fresh", "OPEN", 0, 90, 90, 3)]
    seen, run = [], engine.run

    def watched(argv, **kwargs):
        result = run(argv, **kwargs)
        if "init-agent" not in argv:
            return result
        seen.append(engine._quota_accounts[0].sessions)
        return type(result)(returncode=1, stdout="", stderr="herdr down") if fail else result

    engine.run = watched
    return engine, config, task, seen


def test_the_herdr_runtime_reserves_the_account_seat_before_it_launches(launching):
    engine, config, task, seen = _quota_launch(launching)
    engine.spawn(config, "eng", "worker", task)
    assert seen == [1]
    assert engine._quota_accounts[0].sessions == 1


def test_a_failed_herdr_launch_gives_its_account_seat_back(launching):
    engine, config, task, seen = _quota_launch(launching, fail=True)
    with pytest.raises(SpawnError):
        engine.spawn(config, "eng", "worker", task)
    assert seen == [1]
    assert engine._quota_accounts[0].sessions == 0


def test_the_herdr_launch_room_is_the_free_quota_seats(tmp_path):
    engine = runtime.HerdrRuntime(home=tmp_path, run=None)
    assert engine.launch_room(None) == 1
    engine._quota_accounts = [
        capacity.Account("claude", "one", "OPEN", 1, 90, 90, 3),
        capacity.Account("codex", "two", "OPEN", 0, 90, 90, 2),
        capacity.Account("claude", "full", "OPEN", 4, 90, 90, 4),
    ]
    assert engine.launch_room(None) == 4


def test_the_fenced_runtime_passes_the_launch_room_through(store):
    from scripts.swarm import controller

    held = object()
    assert controller.FencedRuntime(store, "sw", held, PooledRuntime(5), controller.LOCAL).launch_room(None) == 5
    assert controller.FencedRuntime(store, "sw", held, FakeRuntime(), controller.LOCAL).launch_room(None) == 1
