import pytest

from scripts.swarm.tick import tick
from tests.swarm.test_tick import FailingFor, FakeRuntime, _slow_launches, store, tasks  # noqa: F401

pytestmark = pytest.mark.unit


def test_an_urgent_head_of_a_busy_lane_launches_before_lower_ranked_lanes_in_a_one_launch_budget(
    store,  # noqa: F811
    monkeypatch,
):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    ledger.rows.update(tasks(("t2", "ci"), ("t3", "eng")).rows)
    ledger.rows["t3"]["rank"] = "urgent"
    _slow_launches(monkeypatch, runtime, 20)
    actions = tick("sw", store, ledger, runtime, now_ms=2_000)
    assert [task for _, _, task in runtime.spawned] == ["t1", "t3"]
    assert "spent this pass's 20 second launch budget, t2 waits a pass" in actions
    assert ledger.rows["t2"]["state"] == "open"


def test_a_lane_task_whose_launch_failed_spends_no_budget_ahead_of_the_head(store, monkeypatch):  # noqa: F811
    ledger, runtime = tasks(("t1", "eng")), FailingFor("t2")
    tick("sw", store, ledger, runtime, now_ms=1_000)
    ledger.rows.update(tasks(("t2", "ci"), ("t3", "eng")).rows)
    monkeypatch.setattr(store, "launch_failure", lambda slug, task_id: "boom" if task_id == "t2" else None)
    _slow_launches(monkeypatch, runtime, 20)
    actions = tick("sw", store, ledger, runtime, now_ms=2_000)
    assert runtime.tried == ["t1", "t3"]
    assert "spent this pass's 20 second launch budget, t2 waits a pass" in actions
