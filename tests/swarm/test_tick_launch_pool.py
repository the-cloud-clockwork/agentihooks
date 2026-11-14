import threading
import time
from pathlib import Path

import pytest

from scripts.swarm import capacity, runtime
from scripts.swarm import tick as tick_module
from scripts.swarm.tick import SpawnError, tick
from tests.swarm.test_host_gate import _decide, _held, _ledger, _workers
from tests.swarm.test_tick import FakeRuntime, tasks
from tests.swarm.test_tick import store as store
from tests.test_profile_binding_transfers import launching as launching


class PooledRuntime(FakeRuntime):
    def __init__(self, room, hold=0.0, barrier=None, reach=0):
        super().__init__()
        self.room, self.hold, self.barrier, self.reach = room, hold, barrier, reach
        self.lock, self.running, self.peak = threading.Condition(), 0, 0

    def launch_room(self, config):
        return self.room

    def spawn(self, config, lane, name, task):
        if lane == "master":
            return super().spawn(config, lane, name, task)
        with self.lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
            self.lock.notify_all()
            self.lock.wait_for(lambda: self.peak >= self.reach, timeout=2)
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


@pytest.mark.feature("swarm-tick-launch-pool-legacy")
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


@pytest.mark.feature("swarm-tick-launch-pool-legacy")
def test_launches_in_flight_count_against_the_host_room(store):
    ledger, pooled = _ledger(), PooledRuntime(8)
    _decide(store, 2)
    actions = tick("sw", store, ledger, pooled, now_ms=1_000)
    assert _workers(pooled) == ["t1"]
    assert _held(2, 2) in actions


@pytest.mark.feature("swarm-tick-launch-outcomes-legacy")
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


@pytest.mark.feature("swarm-tick-launch-outcomes-legacy")
def test_a_task_that_fails_before_its_launch_reopens_and_the_pool_launches_the_rest(store, monkeypatch):
    ledger, pooled = _eight(store), PooledRuntime(8)
    attach = tick_module.transfers.attach

    def attaching(store_, slug, record):
        if record.task == "t2":
            raise RuntimeError("attach broke")
        return attach(store_, slug, record)

    monkeypatch.setattr(tick_module.transfers, "attach", attaching)
    actions = tick("sw", store, ledger, pooled, now_ms=1_000)
    assert sorted(task for _, _, task in pooled.spawned) == [f"t{n}" for n in range(1, 9) if n != 2]
    assert [action for action in actions if action.startswith("spawn failed for t2")][0].endswith(": attach broke")
    assert ledger.rows["t2"]["state"] == "open"
    assert store.claimant("sw", "t2") is None


@pytest.mark.feature("swarm-tick-launch-pool-legacy")
def test_a_launch_names_the_overlapping_peer_launched_beside_it(store):
    from tests.swarm.test_tick import FakeLedger

    ledger = FakeLedger([{"id": "t1", "territory": ["hooks"]}, {"id": "t2", "territory": ["hooks/x.py"]}])
    pooled = PooledRuntime(8)
    tick("sw", store, ledger, pooled, now_ms=1_000)
    sent = {task["id"]: task for task in pooled.tasks}
    claimant = ledger.rows["t1"]["claimed_by"]
    assert sent["t2"]["overlaps"] == [{"task": "t1", "claimant": claimant, "areas": ["hooks/x.py"]}]
    assert sent["t1"]["overlaps"] == []


@pytest.mark.feature("swarm-tick-launch-pool-legacy")
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


def test_the_herdr_launch_room_is_the_free_quota_seats(tmp_path: Path) -> None:
    engine = runtime.HerdrRuntime(home=tmp_path)
    assert engine.launch_room(None) == 1
    engine = runtime.HerdrRuntime(
        home=tmp_path,
        accounts=[
            capacity.Account("claude", "one", "OPEN", 1, 90, 90, 3),
            capacity.Account("codex", "two", "OPEN", 0, 90, 90, 2),
            capacity.Account("claude", "full", "OPEN", 4, 90, 90, 4),
        ],
    )
    assert engine.launch_room(None) == 4


def test_the_fenced_runtime_passes_the_launch_room_through(store):
    from scripts.swarm import controller

    held = object()
    assert controller.FencedRuntime(store, "sw", held, PooledRuntime(5), controller.LOCAL).launch_room(None) == 5
    assert controller.FencedRuntime(store, "sw", held, FakeRuntime(), controller.LOCAL).launch_room(None) == 1


class ConfigRoom(PooledRuntime):
    def launch_room(self, config):
        return config.max_eng if config is not None else 1


def test_the_fenced_runtime_asks_the_room_with_the_config_it_was_given(store):
    from scripts.swarm import controller

    store.update("sw", max_eng=6)
    fenced = controller.FencedRuntime(store, "sw", object(), ConfigRoom(0), controller.LOCAL)
    assert fenced.launch_room(store.config("sw")) == 6
