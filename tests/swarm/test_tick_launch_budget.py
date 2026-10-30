import pytest

from scripts.swarm.store import MASTER
from scripts.swarm.tick import SpawnError, tick
from tests.swarm.test_tick import FakeRuntime, _slow_launches, tasks
from tests.swarm.test_tick import store as store


@pytest.fixture(autouse=True)
def twenty_second_budget(monkeypatch):
    monkeypatch.setattr("scripts.swarm.tick.SPAWN_BUDGET_S", 20)


@pytest.mark.parametrize("autonomy", ["assist", "delegate"])
@pytest.mark.parametrize("urgent_lane,occupied", [("ci", False), ("eng", True)])
@pytest.mark.parametrize("fail_lower", [False, True])
def test_the_urgent_head_spends_the_shared_launch_budget_first(
    store, monkeypatch, autonomy, urgent_lane, occupied, fail_lower
):
    store.update("sw", autonomy=autonomy)
    ledger, runtime = tasks(), FakeRuntime()
    if occupied:
        ledger.rows.update(tasks(("active", "eng")).rows)
        tick("sw", store, ledger, runtime, now_ms=1_000)
    lower_lane = "ci" if urgent_lane == "eng" else "eng"
    ledger.rows.update(tasks(("lower", lower_lane), ("urgent", urgent_lane)).rows)
    ledger.rows["urgent"]["rank"] = "urgent"
    attempts, spawn = [], runtime.spawn

    def launch(config, lane, name, task):
        if lane != MASTER:
            attempts.append(task["id"])
        if fail_lower and task["id"] == "lower":
            raise SpawnError("no hive accepts this placement")
        return spawn(config, lane, name, task)

    monkeypatch.setattr(runtime, "spawn", launch)
    _slow_launches(monkeypatch, runtime, 12)
    actions = tick("sw", store, ledger, runtime, now_ms=2_000)
    assert attempts == ["urgent"]
    assert runtime.spawned[-1][2] == "urgent"
    assert ledger.rows["lower"]["state"] == "open"
    assert store.claimant("sw", "lower") is None
    assert "spent this pass's 20 second launch budget, lower waits a pass" in actions


@pytest.mark.parametrize("autonomy", ["assist", "delegate"])
@pytest.mark.parametrize("rank,expected,deferred", [("normal", "healthy", "failed"), ("urgent", "failed", "healthy")])
def test_recorded_launch_failures_follow_healthy_tasks_only_within_their_band(
    store, monkeypatch, autonomy, rank, expected, deferred
):
    store.update("sw", autonomy=autonomy)
    ledger, runtime = tasks(("failed", "eng"), ("healthy", "ci")), FakeRuntime()
    ledger.rows["failed"]["rank"] = rank
    store.note_launch_failure("sw", "failed", "placement refused")
    _slow_launches(monkeypatch, runtime, 12)
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert [task for _, _, task in runtime.spawned] == [expected]
    assert ledger.rows[deferred]["state"] == "open"
    assert store.claimant("sw", deferred) is None
    assert f"spent this pass's 20 second launch budget, {deferred} waits a pass" in actions


class PooledLaunches(FakeRuntime):
    def launch_room(self, config):
        return 8


class PooledFailures(PooledLaunches):
    def __init__(self):
        super().__init__()
        self.tried = []

    def spawn(self, config, lane, name, task):
        if lane == MASTER:
            return super().spawn(config, lane, name, task)
        self.tried.append(task["id"])
        raise SpawnError("no hive accepts this placement")


def _slow_preparation(monkeypatch, clock, seconds):
    from scripts.swarm import tick as tick_module

    prepare = tick_module._prepare

    def slow(*args):
        clock[0] += seconds
        return prepare(*args)

    monkeypatch.setattr(tick_module, "_prepare", slow)


@pytest.mark.feature("swarm-tick-launch-budget")
def test_a_slow_preparation_stops_the_pass_at_the_launch_budget(store, monkeypatch):
    ids = [f"t{n}" for n in range(1, 9)]
    store.update("sw", max_eng=len(ids))
    ledger, runtime = tasks(*((i, "eng") for i in ids)), PooledLaunches()
    clock = _slow_launches(monkeypatch, runtime, 0)
    _slow_preparation(monkeypatch, clock, 18)
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert [task for _, _, task in runtime.spawned] == ["t1"]
    assert "spent this pass's 20 second launch budget, t2 waits a pass" in actions
    assert ledger.rows["t2"]["state"] == "open"
    assert store.claimant("sw", "t2") is None


def test_a_pass_over_many_agents_with_failing_launches_ends_under_the_tick_deadline(store, monkeypatch):
    from scripts.swarm.tick_stop import DEADLINE_S

    ids = [f"t{n}" for n in range(1, 33)]
    store.update("sw", max_eng=len(ids))
    ledger, runtime = tasks(*((i, "eng") for i in ids)), PooledFailures()
    clock = _slow_launches(monkeypatch, runtime, 20)
    _slow_preparation(monkeypatch, clock, 18)
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert runtime.tried == ["t1"]
    assert clock[0] < DEADLINE_S


class PairedFailures(PooledFailures):
    def launch_room(self, config):
        return 2


def test_a_forgiven_failed_pool_keeps_its_preparation_time_on_the_budget(store, monkeypatch):
    store.update("sw", max_eng=3)
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng"), ("t3", "eng")), PairedFailures()
    clock = _slow_launches(monkeypatch, runtime, 60)
    _slow_preparation(monkeypatch, clock, 9)
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert runtime.tried == ["t1", "t2"]
    assert "spent this pass's 20 second launch budget, t3 waits a pass" in actions
    assert ledger.rows["t3"]["state"] == "open"


def test_time_before_the_first_launch_never_holds_back_a_filling_pool(store, monkeypatch):
    from scripts.swarm import tick as tick_module

    store.update("sw", max_eng=3)
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng"), ("t3", "eng")), PooledLaunches()
    clock = _slow_launches(monkeypatch, runtime, 0)
    stop = tick_module._spawn_stop

    def slow_stop(*args):
        clock[0] += 30.0
        return stop(*args)

    monkeypatch.setattr(tick_module, "_spawn_stop", slow_stop)
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert sorted(task for _, _, task in runtime.spawned) == ["t1", "t2", "t3"]
    assert not any("launch budget" in action for action in actions)


def test_a_filling_pool_is_priced_at_its_mean_preparation_time(store, monkeypatch):
    store.update("sw", max_eng=3)
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng"), ("t3", "eng")), PooledLaunches()
    clock = _slow_launches(monkeypatch, runtime, 0)
    _slow_preparation(monkeypatch, clock, 7)
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert sorted(task for _, _, task in runtime.spawned) == ["t1", "t2"]
    assert "spent this pass's 20 second launch budget, t3 waits a pass" in actions


def test_preparations_whose_mean_fits_the_rest_of_the_budget_fill_the_pool(store, monkeypatch):
    store.update("sw", max_eng=4)
    ledger, runtime = tasks(*((f"t{n}", "eng") for n in range(1, 5))), PooledLaunches()
    clock = _slow_launches(monkeypatch, runtime, 0)
    _slow_preparation(monkeypatch, clock, 3)
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert sorted(task for _, _, task in runtime.spawned) == ["t1", "t2", "t3", "t4"]
    assert not any("launch budget" in action for action in actions)


class PairedLaunches(PooledLaunches):
    def launch_room(self, config):
        return 2


def test_a_second_pool_fills_on_preparation_time_once_its_first_task_is_priced(store, monkeypatch):
    store.update("sw", max_eng=4)
    ledger, runtime = tasks(*((f"t{n}", "eng") for n in range(1, 5))), PairedLaunches()
    clock = _slow_launches(monkeypatch, runtime, 3.5)
    _slow_preparation(monkeypatch, clock, 1)
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert sorted(task for _, _, task in runtime.spawned) == ["t1", "t2", "t3", "t4"]
    assert not any("launch budget" in action for action in actions)
