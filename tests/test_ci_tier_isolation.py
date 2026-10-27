from pathlib import Path

import pytest
import yaml

from scripts import ci_gate_queue
from tests.contracts.ci_tiers.test_workflow import _concurrency
from tests.test_ci_gate_queue import complete

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def test_promotion_grades_new_heads_without_waiting_for_an_older_red_run():
    first = _concurrency("push", 1000)
    second = _concurrency("push", 1061)
    assert first == {"group": "tests-push-1000", "queue": "single", "cancel-in-progress": False}
    assert second == {"group": "tests-push-1061", "queue": "single", "cancel-in-progress": False}
    release = yaml.safe_load((ROOT / ".github/workflows/promote.yml").read_text())
    assert release["jobs"]["promote"]["concurrency"] == {
        "group": "release-version",
        "cancel-in-progress": False,
    }


@pytest.mark.parametrize("name", sorted(ci_gate_queue.SKIPPED))
@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out"])
def test_unrelated_red_check_does_not_hold_a_green_merge_group(name, conclusion):
    jobs = complete()
    next(job for job in jobs if job["name"] == name)["conclusion"] = conclusion
    assert ci_gate_queue.verify(jobs, [2, 7])


def test_queue_does_not_wait_for_jobs_outside_its_tier():
    jobs = [job for job in complete() if job["name"] not in ci_gate_queue.SKIPPED]
    assert ci_gate_queue.verify(jobs, [2, 7])


@pytest.mark.parametrize("name", ["select", "split", "test-count", "shard-check (3.12)", "unit (3.12, 2)"])
@pytest.mark.parametrize("state", ["failure", "missing", "pending"])
def test_required_queue_checks_still_hold_with_unrelated_red_work(name, state):
    jobs = complete()
    next(job for job in jobs if job["name"] == "sonar")["conclusion"] = "failure"
    target = next(job for job in jobs if job["name"] == name)
    if state == "missing":
        jobs.remove(target)
    elif state == "pending":
        target.update(status="in_progress", conclusion=None)
    else:
        target["conclusion"] = "failure"
    if state == "failure":
        with pytest.raises(ValueError, match=name.split(" (")[0]):
            ci_gate_queue.verify(jobs, [2, 7])
    else:
        assert ci_gate_queue.verify(jobs, [2, 7]) is False


def test_unrelated_red_work_cannot_allow_a_stale_queue_head(monkeypatch):
    jobs = complete()
    next(job for job in jobs if job["name"] == "sonar")["conclusion"] = "failure"
    monkeypatch.setattr(ci_gate_queue, "read_jobs", lambda endpoint, size: jobs)
    monkeypatch.setattr(
        ci_gate_queue,
        "api",
        lambda endpoint: [[{"ref": "refs/queue", "object": {"sha": "different-head"}}]],
    )
    with pytest.raises(ValueError, match="[Qq]ueue group"):
        ci_gate_queue.watch("owner/repo", "123", "2", [2, 7], "refs/queue", "head")
