import threading
import time
from pathlib import Path

import pytest

from scripts.swarm import capacity, runtime
from scripts.swarm.tick import SpawnError
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
