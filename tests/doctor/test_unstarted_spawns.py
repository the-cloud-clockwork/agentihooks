import pytest

from scripts.doctor import spawns


@pytest.mark.parametrize(
    ("fields", "summary"),
    [
        ({"state": "starting"}, "starting launch has no recorded harness or pane"),
        ({"state": "working"}, "spawned agent has run no tool since it started"),
        ({"state": "starting", "harness": "claude"}, "spawned agent has run no tool since it started"),
        ({"state": "starting", "pane_id": "w1:p1"}, "spawned agent has run no tool since it started"),
    ],
)
def test_hook_finding_distinguishes_an_unstarted_launch_without_changing_its_measure(fields, summary):
    agent = {"name": "ci@a-1", "started_at": 1_000, **fields}
    [found] = spawns.silent_starts([agent], {}, 1_000 + 6 * spawns.TICK_MS)
    assert found.id == "no-hook-event/ci@a-1"
    assert found.summary == summary
    assert found.measure == 6
    assert found.threshold == "no hook event one tick after spawn"
