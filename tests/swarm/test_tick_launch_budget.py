import pytest

from scripts.swarm.tick import SpawnError, tick
from tests.swarm.test_tick import FakeRuntime, _slow_launches, tasks
from tests.swarm.test_tick import store as store


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
