import copy

import pytest

from scripts import ci_gate_queue

pytestmark = pytest.mark.unit


def complete():
    passed = ["select", "split", "test-count", "shard-check (3.12)", "unit (3.12, 2)", "unit (3.12, 7)"]
    skipped = [
        "unit-lost",
        "unit rerun (${{ matrix.python-version }}, ${{ matrix.shard }})",
        "unit-selected",
        "coverage-ratchet",
        "lint",
        "docs",
        "sonar",
        "mutation-plan",
        "mutation-stats",
        "mutation",
        "swarm-image",
        "worker-image",
        "brain-smoke",
        "writer-lock",
        "kind-due",
        "helm-kind",
        "semgrep",
        "ledger-load",
        "stage-budget",
        "reuse",
    ]
    return [
        {"id": index, "name": name, "status": "completed", "conclusion": result}
        for index, (name, result) in enumerate(
            [(name, "success") for name in passed] + [(name, "skipped") for name in skipped], 1
        )
    ]


def test_complete_queue_waits_for_the_shard_judge_and_every_inventory_row():
    jobs = complete()
    assert ci_gate_queue.verify(jobs, [2, 7])
    for index in range(len(jobs)):
        assert not ci_gate_queue.verify(jobs[:index] + jobs[index + 1 :], [2, 7])
        pending = copy.deepcopy(jobs)
        pending[index].update(status="in_progress", conclusion=None)
        assert not ci_gate_queue.verify(pending, [2, 7])


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "neutral", "skipped", None])
@pytest.mark.parametrize("name", ["select", "split", "test-count", "shard-check (3.12)", "unit (3.12, 7)"])
def test_a_failed_or_skipped_required_job_never_passes(name, conclusion):
    jobs = complete()
    next(job for job in jobs if job["name"] == name)["conclusion"] = conclusion
    with pytest.raises(ValueError):
        ci_gate_queue.verify(jobs, [2, 7])


@pytest.mark.parametrize("name", ["lint", "reuse", "sonar", "unit-selected", "mutation", "helm-kind"])
def test_an_unexpected_running_promotion_or_pull_request_gate_never_passes(name):
    jobs = complete()
    next(job for job in jobs if job["name"] == name)["conclusion"] = "success"
    with pytest.raises(ValueError):
        ci_gate_queue.verify(jobs, [2, 7])


@pytest.mark.parametrize("name", ["select", "unit (3.12, 2)", "shard-check (3.12)"])
def test_duplicate_or_unplanned_shards_are_rejected(name):
    jobs = complete()
    duplicate = dict(next(job for job in jobs if job["name"] == name), id=900)
    with pytest.raises(ValueError):
        ci_gate_queue.verify(jobs + [duplicate], [2, 7])
    for extra in ["unit (3.12, 3)", "unit (3.11, 2)", "unit rerun (3.12, 3)"]:
        with pytest.raises(ValueError):
            ci_gate_queue.verify(jobs + [dict(duplicate, name=extra)], [2, 7])


@pytest.mark.parametrize("shards", [[], [2, 2], [0], [-1], ["2"], [True]])
def test_a_missing_or_invalid_split_plan_is_red(shards):
    with pytest.raises(ValueError):
        ci_gate_queue.verify(complete(), shards)


@pytest.mark.parametrize("original", ["failure", "cancelled"])
def test_recovery_requires_the_matching_successful_rerun_and_confirmed_loss(original):
    jobs = complete()
    next(job for job in jobs if job["name"] == "unit (3.12, 7)")["conclusion"] = original
    next(job for job in jobs if job["name"] == "unit-lost")["conclusion"] = "success"
    jobs = [job for job in jobs if not job["name"].startswith("unit rerun")]
    rerun = {"id": 100, "name": "unit rerun (3.12, 7)", "status": "completed", "conclusion": "success"}
    assert not ci_gate_queue.verify(jobs, [2, 7], lost=lambda job: True)
    assert ci_gate_queue.verify(jobs + [rerun], [2, 7], lost=lambda job: True)
    with pytest.raises(ValueError):
        ci_gate_queue.verify(jobs + [rerun], [2, 7], lost=lambda job: False)
    for result in ["failure", "cancelled", "skipped"]:
        with pytest.raises(ValueError):
            ci_gate_queue.verify(jobs + [dict(rerun, conclusion=result)], [2, 7], lost=lambda job: True)


def test_only_the_reviewed_pair_of_event_names_and_canonical_watcher_normalizes():
    watcher = ci_gate_queue.watcher("runner")
    workflow = {
        "test.yml": {
            "jobs": {
                "gate-required": {"name": ci_gate_queue.SUMMARY_NAME, "runs-on": "runner"},
                "queue-fail-fast": watcher,
            }
        }
    }
    normalized = ci_gate_queue.normalize(workflow)
    assert normalized["test.yml"]["jobs"]["gate-required"]["name"] == "Gate — Required"
    assert workflow["test.yml"]["jobs"]["gate-required"]["name"] == ci_gate_queue.SUMMARY_NAME
    for field, value in [("needs", []), ("steps", []), ("if", "true"), ("timeout-minutes", 100), ("runs-on", "other")]:
        changed = copy.deepcopy(workflow)
        changed["test.yml"]["jobs"]["queue-fail-fast"][field] = value
        assert ci_gate_queue.normalize(changed) == changed
    changed = copy.deepcopy(workflow)
    changed["test.yml"]["jobs"]["gate-required"]["name"] = "other"
    assert ci_gate_queue.normalize(changed) == changed
