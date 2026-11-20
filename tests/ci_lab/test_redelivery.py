"""A lab worker killed mid unit stops heartbeating, so its lease expires and the scheduler redelivers the unit once;
the dead worker's token can neither extend nor finish it, a second worker finishes it, nothing counts as lost, and the
gate and Gate Required each post one completed check run."""

from pathlib import Path

import pytest

from scripts.ci_lab import scheduler
from scripts.ci_lab.objects import LocalObjects
from scripts.ci_lab.retry import Policy
from scripts.ci_lab.store import Verdict
from tests.ci_lab.test_scheduler import HEAD_A, Expander, Inbox, Lab, Run, fake_redis, plan, scrape, unit


@pytest.mark.feature("lab-worker-loss-redelivery")
def test_a_worker_killed_mid_unit_is_redelivered_once_and_its_verdict_posts_once(tmp_path: Path) -> None:
    item = unit(1)
    state = Lab(fake_redis(), LocalObjects(tmp_path / "objects"), Inbox(), Expander({HEAD_A: plan({"lint": (item,)})}))
    run = Run(state, state.build(0, Policy(), None))
    run.push(HEAD_A)
    run.tick(1000)
    killed = run.lease(ttl_ms=100)[0]
    run.tick(1099)
    assert run.tick(1100) == [f"reaped {killed.job} pending"]
    (second,) = run.leases(ttl_ms=100).lease("worker-2", 1, frozenset(), 0)
    assert second.job == killed.job
    assert run.leases().heartbeat(killed.job, killed.token) is None
    assert run.leases(ttl_ms=100).heartbeat(second.job, second.token) == 1200
    assert not run.leases().complete(killed.job, killed.token, "passed")
    run.finish(second, "passed", {})
    run.tick(1200)
    moves = [("queued", None), ("in_progress", None), ("completed", "success")]
    assert run.lab.poster.moves(HEAD_A, "lint") == moves
    assert run.lab.poster.moves(HEAD_A, scheduler.REQUIRED) == moves
    assert run.grade(HEAD_A).conclusion == "success"
    assert run.store.get(item.kind, item.key) == Verdict(item.kind, item.key, True, False, 0.0)
    text = scrape(run, 1200)
    assert "\nci_lab_units_redelivered_total 1.0\n" in text
    assert "\n# TYPE ci_lab_units_lost_total counter\n" in text
    assert "\nci_lab_units_lost_total " not in text
