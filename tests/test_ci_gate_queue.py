import copy
import re

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


@pytest.mark.parametrize("conclusion", ["success", "failure", "cancelled", "skipped"])
def test_parallel_split_replaces_only_the_serial_split_grade(conclusion):
    jobs = complete()
    next(job for job in jobs if job["name"] == "split")["conclusion"] = "skipped"
    jobs.append({"id": 1000, "name": "queue-split (3.12)", "status": "completed", "conclusion": conclusion})
    if conclusion == "success":
        assert ci_gate_queue.verify(jobs, [2, 7])
    else:
        with pytest.raises(ValueError, match="queue-split"):
            ci_gate_queue.verify(jobs, [2, 7])


@pytest.mark.parametrize("conclusion", ["success", "failure", "cancelled"])
def test_parallel_split_keeps_the_other_tiers_excluded(conclusion):
    jobs = complete()
    next(job for job in jobs if job["name"] == "split")["conclusion"] = "skipped"
    jobs.append({"id": 1000, "name": "queue-split (3.12)", "status": "completed", "conclusion": "success"})
    next(job for job in jobs if job["name"] == "lint")["conclusion"] = conclusion
    with pytest.raises(ValueError, match="lint"):
        ci_gate_queue.verify(jobs, [2, 7])


@pytest.mark.parametrize("state", ["missing", "queued", "in_progress", "duplicate"])
def test_parallel_split_cannot_pass_without_one_completed_producer(state):
    jobs = complete()
    next(job for job in jobs if job["name"] == "split")["conclusion"] = "skipped"
    producer = {"id": 1000, "name": "queue-split (3.12)", "status": "completed", "conclusion": "success"}
    if state != "missing":
        jobs.append(dict(producer, status=state if state != "duplicate" else "completed"))
    if state == "duplicate":
        jobs.append(dict(producer, id=1001))
    if state in {"missing", "duplicate"}:
        with pytest.raises(ValueError):
            ci_gate_queue.verify(jobs, [2, 7])
    else:
        assert ci_gate_queue.verify(jobs, [2, 7]) is False


def test_complete_queue_waits_for_the_shard_judge_and_every_inventory_row():
    jobs = complete()
    assert ci_gate_queue.verify(jobs, [2, 7])
    for index in range(len(jobs)):
        assert ci_gate_queue.verify(jobs[:index] + jobs[index + 1 :], [2, 7]) is False
        pending = copy.deepcopy(jobs)
        pending[index].update(status="in_progress", conclusion=None)
        assert ci_gate_queue.verify(pending, [2, 7]) is False


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "neutral", "skipped", None])
@pytest.mark.parametrize("name", ["select", "split", "test-count", "shard-check (3.12)", "unit (3.12, 7)"])
def test_a_failed_or_skipped_required_job_never_passes(name, conclusion):
    jobs = complete()
    next(job for job in jobs if job["name"] == name)["conclusion"] = conclusion
    with pytest.raises(ValueError) as failure:
        ci_gate_queue.verify(jobs, [2, 7])
    assert name in str(failure.value)


@pytest.mark.parametrize("name", ["lint", "reuse", "sonar", "unit-selected", "mutation", "helm-kind"])
def test_an_unexpected_running_promotion_or_pull_request_gate_never_passes(name):
    jobs = complete()
    next(job for job in jobs if job["name"] == name)["conclusion"] = "success"
    with pytest.raises(ValueError) as failure:
        ci_gate_queue.verify(jobs, [2, 7])
    assert name in str(failure.value)


@pytest.mark.parametrize("name", ["select", "unit (3.12, 2)", "shard-check (3.12)"])
def test_duplicate_or_unplanned_shards_are_rejected(name):
    jobs = complete()
    duplicate = dict(next(job for job in jobs if job["name"] == name), id=900)
    with pytest.raises(ValueError, match=re.escape(name)):
        ci_gate_queue.verify(jobs + [duplicate], [2, 7])
    for extra in ["unit (3.12, 3)", "unit (3.11, 2)", "unit rerun (3.12, 3)"]:
        with pytest.raises(ValueError, match=re.escape(extra)):
            ci_gate_queue.verify(jobs + [dict(duplicate, name=extra)], [2, 7])


@pytest.mark.parametrize("shards", [[], [2, 2], [0], [-1], ["2"], [True]])
def test_a_missing_or_invalid_split_plan_is_red(shards):
    with pytest.raises(ValueError, match=re.compile("nonempty.*unique.*positive", re.I)):
        ci_gate_queue.verify(complete(), shards)


@pytest.mark.parametrize("original", ["failure", "cancelled"])
def test_recovery_requires_the_matching_successful_rerun_and_confirmed_loss(original):
    jobs = complete()
    next(job for job in jobs if job["name"] == "unit (3.12, 7)")["conclusion"] = original
    next(job for job in jobs if job["name"] == "unit-lost")["conclusion"] = "success"
    jobs = [job for job in jobs if not job["name"].startswith("unit rerun")]
    rerun = {"id": 100, "name": "unit rerun (3.12, 7)", "status": "completed", "conclusion": "success"}
    assert ci_gate_queue.verify(jobs, [2, 7], lost=lambda job: True) is False
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
                "gate-required": {
                    "name": ci_gate_queue.SUMMARY_NAME,
                    "runs-on": "runner",
                    "needs": [
                        "select",
                        "split",
                        "unit",
                        "unit-lost",
                        "unit-rerun",
                        "unit-selected",
                        "shard-check",
                        "test-count",
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
                        "queue-fail-fast",
                    ],
                },
                "queue-fail-fast": watcher,
                "reuse": {"if": "github.event_name != 'merge_group' && github.event_name != 'pull_request'"},
            }
        }
    }
    normalized = ci_gate_queue.normalize(workflow)
    assert normalized["test.yml"]["jobs"]["gate-required"]["name"] == "Gate — Required"
    assert normalized["test.yml"]["jobs"]["queue-fail-fast"]["name"] == "queue-fail-fast"
    assert workflow["test.yml"]["jobs"]["gate-required"]["name"] == ci_gate_queue.SUMMARY_NAME
    for field, value in [("needs", []), ("steps", []), ("if", "true"), ("timeout-minutes", 100), ("runs-on", "other")]:
        changed = copy.deepcopy(workflow)
        changed["test.yml"]["jobs"]["queue-fail-fast"][field] = value
        with pytest.raises(ValueError):
            ci_gate_queue.normalize(changed)
    changed = copy.deepcopy(workflow)
    changed["test.yml"]["jobs"]["gate-required"]["name"] = "other"
    with pytest.raises(ValueError):
        ci_gate_queue.normalize(changed)


def test_the_early_watcher_binds_the_plan_and_uses_protected_code_and_app_quota():
    job = ci_gate_queue.watcher("runner")
    assert job["name"] == "${{ github.event_name == 'merge_group' && 'Gate — Required' || 'queue-fail-fast' }}"
    assert (
        ci_gate_queue.SUMMARY_NAME
        == "${{ github.event_name == 'merge_group' && 'Queue gate summary' || 'Gate — Required' }}"
    )
    assert job["needs"] == ["split", "select", "reuse", "admission"]
    assert job["runs-on"] == "runner"
    assert job["timeout-minutes"] == 60
    assert job["outputs"] == {"failed": "${{ steps.watch.outputs.failed }}"}
    assert (
        job["if"]
        == "${{ !cancelled() && (github.event_name != 'workflow_dispatch' || needs.admission.result == 'success') && (github.event_name != 'merge_group' || needs.reuse.outputs.reused != 'true') && github.event_name != 'pull_request' }}"
    )
    checkout, token, watch, cancel = job["steps"]
    assert set(token) == {"name", "id", "if", "uses", "with"}
    assert set(watch) == {"name", "id", "if", "env", "run"}
    assert set(cancel) == {"name", "if", "env", "run"}
    assert "token" in token["name"].lower()
    assert "queue gate" in watch["name"].lower()
    assert "queue run" in cancel["name"].lower()
    assert checkout == {
        "uses": "actions/checkout@v4",
        "if": "github.event_name == 'merge_group'",
        "with": {"ref": "${{ github.event.merge_group.base_sha }}", "persist-credentials": False},
    }
    assert token["uses"] == "actions/create-github-app-token@v3.2.0"
    assert token["id"] == "app-token"
    assert token["if"] == "github.event_name == 'merge_group'"
    assert token["with"] == {
        "client-id": "${{ secrets.TCC_CI_CLIENT_ID }}",
        "private-key": "${{ secrets.TCC_CI_APP_PRIVATE_KEY }}",
        "repositories": "${{ github.event.repository.name }}",
        "permission-actions": "write",
        "permission-contents": "read",
        "permission-pull-requests": "read",
    }
    assert watch["id"] == "watch"
    assert watch["if"] == "github.event_name == 'merge_group'"
    assert watch["env"] == {
        "GH_TOKEN": "${{ steps.app-token.outputs.token }}",
        "QUEUE_PLAN": "${{ format('[{0}]', needs.split.outputs.queue_312) }}",
    }
    assert (
        watch["run"]
        == 'python3 -m scripts.ci_gate_queue || { echo "failed=queue verification" >> "$GITHUB_OUTPUT"; exit 1; }'
    )
    assert cancel["if"] == "failure() && github.event_name == 'merge_group'"
    assert cancel["env"] == {"GH_TOKEN": "${{ steps.app-token.outputs.token }}"}
    assert cancel["run"] == 'gh run cancel "$GITHUB_RUN_ID" -R "$GITHUB_REPOSITORY"'


def test_protected_grading_accepts_the_exact_early_pair_and_rejects_any_weakened_watcher():
    import json
    from datetime import date
    from pathlib import Path

    from scripts import ci_runner_trust, ci_wiring, gate_protected

    root = Path(__file__).resolve().parents[1]
    policy = json.loads((root / ci_runner_trust.ACTORS).read_text())
    workflows = ci_wiring.load(root)
    config = json.loads((root / ci_wiring.CONFIG).read_text())
    candidate = copy.deepcopy(workflows)
    jobs = candidate["test.yml"]["jobs"]
    jobs["gate-required"]["name"] = ci_gate_queue.SUMMARY_NAME
    jobs["queue-fail-fast"] = ci_gate_queue.watcher(jobs["gate-required"]["runs-on"], parallel="queue-split" in jobs)
    today = date(2026, 10, 25)
    assert gate_protected.grade(workflows, config, candidate, config, today, policy) == []
    assert ci_wiring.check(candidate, config, today) == []
    for field, value in [("needs", []), ("steps", []), ("if", "true")]:
        changed = copy.deepcopy(candidate)
        changed["test.yml"]["jobs"]["queue-fail-fast"][field] = value
        errors = gate_protected.grade(workflows, config, changed, config, today, policy)
        assert any("context pair" in error.lower() for error in errors)
        errors = ci_wiring.check(changed, config, today)
        assert any("context pair" in error.lower() for error in errors)


def test_api_waiting_shard_verification_remains_a_unit_dependent_for_lost_runner_recovery():
    from scripts import ci_lost_runner, ci_queue_retry

    workflow = {
        "jobs": {
            "unit": {},
            "split": {},
            "shard-check": {"needs": ["split"], "steps": [{"run": "python -m scripts.ci_gate_shards"}]},
            "gate-required": {"name": "Gate — Required", "needs": ["shard-check"]},
        }
    }
    assert {"shard-check", "Gate — Required"} <= ci_queue_retry.dependents(workflow)
    lost = {"name": "unit (3.12, 2)", "status": "in_progress"}
    waiting = {"name": "shard-check (3.12)", "status": "in_progress"}
    assert ci_lost_runner.stalled([lost, waiting], [lost], workflow) == (True, "runner lost on unit (3.12, 2)")


def test_reuse_recognizes_the_nonqueue_required_context_of_the_exact_name_pair():
    from scripts import ci_reuse

    workflow = {"jobs": {"gate-required": {"name": ci_gate_queue.SUMMARY_NAME, "needs": ["unit"]}, "unit": {}}}
    assert ci_reuse._passed(
        workflow, [{"name": "Gate — Required", "conclusion": "success"}, {"name": "unit", "conclusion": "success"}]
    )


def test_watch_waits_for_complete_attempt_jobs_and_rechecks_queue_identity(monkeypatch):
    reads, queries, sleeps = [], [], []
    first = [job for job in complete() if job["name"] != "shard-check (3.12)"]
    snapshots = iter([first, complete()])
    monkeypatch.setattr(
        ci_gate_queue, "read_jobs", lambda endpoint: reads.append(endpoint) or next(snapshots), raising=False
    )
    monkeypatch.setattr(
        ci_gate_queue,
        "api",
        lambda endpoint: (
            queries.append(endpoint)
            or [[{"ref": "refs/heads/gh-readonly-queue/dev/pr-1", "object": {"sha": "queue-head"}}]]
        ),
        raising=False,
    )
    monkeypatch.setattr(ci_gate_queue.time, "sleep", sleeps.append)
    ci_gate_queue.watch("owner/repo", "123", "2", [2, 7], "refs/heads/gh-readonly-queue/dev/pr-1", "queue-head")
    assert reads == ["repos/owner/repo/actions/runs/123/attempts/2/jobs"] * 2
    assert queries == ["repos/owner/repo/git/matching-refs/heads/gh-readonly-queue/dev/pr-1"] * 3
    assert sleeps == [5]


@pytest.mark.parametrize(
    "refs", [[], [[{"ref": "other", "object": {"sha": "head"}}]], [[{"ref": "refs/queue", "object": {"sha": "other"}}]]]
)
def test_a_deleted_or_superseded_queue_group_never_passes(monkeypatch, refs):
    monkeypatch.setattr(ci_gate_queue, "api", lambda endpoint: refs, raising=False)
    with pytest.raises(ValueError, match=re.compile("queue group", re.I)):
        ci_gate_queue.watch("owner/repo", "123", "2", [2, 7], "refs/queue", "head")


def test_missing_jobs_timeout_and_an_api_failure_never_claims_success(monkeypatch):
    monkeypatch.setattr(
        ci_gate_queue, "api", lambda endpoint: [[{"ref": "refs/queue", "object": {"sha": "head"}}]], raising=False
    )
    monkeypatch.setattr(ci_gate_queue, "read_jobs", lambda endpoint: [], raising=False)
    clock = iter([0, 0, 1200])
    monkeypatch.setattr(ci_gate_queue.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(ci_gate_queue.time, "sleep", lambda seconds: None)
    with pytest.raises(TimeoutError, match=re.compile("expected job", re.I)):
        ci_gate_queue.watch("owner/repo", "123", "2", [2, 7], "refs/queue", "head")
    monkeypatch.setattr(ci_gate_queue.time, "monotonic", lambda: 0)
    monkeypatch.setattr(
        ci_gate_queue, "api", lambda endpoint: (_ for _ in ()).throw(RuntimeError("API unavailable")), raising=False
    )
    with pytest.raises(RuntimeError, match="API unavailable"):
        ci_gate_queue.watch("owner/repo", "123", "2", [2, 7], "refs/queue", "head")


def test_the_early_gate_rejects_a_changed_graded_job_inventory():
    from pathlib import Path

    import yaml

    workflow = yaml.safe_load((Path(__file__).resolve().parents[1] / ".github/workflows/test.yml").read_text())
    jobs = workflow["jobs"]
    jobs["gate-required"]["name"] = ci_gate_queue.SUMMARY_NAME
    jobs["queue-fail-fast"] = ci_gate_queue.watcher(jobs["gate-required"]["runs-on"], parallel="queue-split" in jobs)
    assert ci_gate_queue.normalize({"test.yml": workflow})
    jobs["gate-required"]["needs"].append("new-required-check")
    with pytest.raises(ValueError, match="inventory"):
        ci_gate_queue.normalize({"test.yml": workflow})


def test_the_early_gate_accepts_parallel_split_only_with_its_reviewed_context_pair():
    from pathlib import Path

    import yaml

    workflow = yaml.safe_load((Path(__file__).resolve().parents[1] / ".github/workflows/test.yml").read_text())
    jobs = workflow["jobs"]
    jobs["queue-split"] = {"needs": ["reuse", "admission"]}
    if "queue-split" not in jobs["gate-required"]["needs"]:
        jobs["gate-required"]["needs"].append("queue-split")
    jobs["queue-fail-fast"] = ci_gate_queue.watcher(jobs["gate-required"]["runs-on"], parallel=True)
    normalized = ci_gate_queue.normalize({"test.yml": workflow})["test.yml"]["jobs"]
    assert normalized["gate-required"]["name"] == "Gate — Required"
    assert normalized["queue-fail-fast"]["name"] == "queue-fail-fast"
    assert "queue-split" in jobs["queue-fail-fast"]["needs"]
    assert (
        jobs["queue-fail-fast"]["steps"][2]["env"]["QUEUE_PLAN"]
        == "${{ format('[{0}]', needs.queue-split.outputs.queue_312) }}"
    )
    jobs["queue-fail-fast"] = ci_gate_queue.watcher(jobs["gate-required"]["runs-on"])
    with pytest.raises(ValueError, match="context pair"):
        ci_gate_queue.normalize({"test.yml": workflow})


def test_parallel_split_with_missing_aggregation_steps_reaches_the_protected_rejection():
    import copy
    import json
    from datetime import date
    from pathlib import Path

    from scripts import ci_wiring, gate_protected

    root = Path(__file__).resolve().parents[1]
    base = ci_wiring.load(root)
    head = copy.deepcopy(base)
    jobs = head["test.yml"]["jobs"]
    jobs["queue-split"] = {"needs": ["reuse", "admission"]}
    summary = jobs["gate-required"]
    summary["needs"].append("queue-split")
    del summary["steps"]
    jobs["queue-fail-fast"] = ci_gate_queue.watcher(summary["runs-on"], parallel=True)
    normalized = ci_gate_queue.normalize(head)
    assert "steps" not in normalized["test.yml"]["jobs"]["gate-required"]
    config = json.loads((root / ".github/gate-wiring.json").read_text())
    policy = json.loads((root / ".github/workflows/trusted-actors.json").read_text())
    assert any(
        "changes its steps" in error
        for error in gate_protected.grade(base, config, head, config, date(2026, 10, 25), policy)
    )


def test_parallel_split_adoption_passes_the_protected_grader_without_weakening_aggregation():
    import copy
    import json
    from datetime import date
    from pathlib import Path

    from scripts import ci_wiring, gate_protected

    root = Path(__file__).resolve().parents[1]
    base = ci_wiring.load(root)
    head = copy.deepcopy(base)
    jobs = head["test.yml"]["jobs"]
    jobs["queue-split"] = copy.deepcopy(jobs["split"])
    jobs["queue-split"]["needs"] = ["reuse", "admission"]
    jobs["queue-split"]["if"] = "${{ !cancelled() && github.event_name == 'merge_group' }}"
    jobs["split"]["if"] = jobs["split"]["if"].replace(
        "!cancelled()", "github.event_name != 'merge_group' && !cancelled()"
    )
    summary = jobs["gate-required"]
    summary["needs"].append("queue-split")
    jobs["queue-fail-fast"] = ci_gate_queue.watcher(summary["runs-on"], parallel=True)
    step = summary["steps"][0]
    step["run"] = (
        step["run"]
        .replace(
            '["select", "split", "shard-check", "test-count"]',
            '["select", "queue-split", "shard-check", "test-count"]',
        )
        .replace(
            'or (.key == "queue-baseline"',
            'or (.key == "queue-split" and .value.result == "skipped" and $event != "merge_group") or (.key == "queue-baseline"',
        )
    )
    config = json.loads((root / ".github/gate-wiring.json").read_text())
    policy = json.loads((root / ".github/workflows/trusted-actors.json").read_text())
    assert gate_protected.grade(base, config, head, config, date(2026, 10, 25), policy) == []
    step["run"] += "exit 0\n"
    assert any(
        "changes its steps" in error
        for error in gate_protected.grade(base, config, head, config, date(2026, 10, 25), policy)
    )


def test_pending_originals_wait_without_misclassifying_started_recovery():
    jobs = complete()
    next(job for job in jobs if job["name"] == "unit (3.12, 7)").update(status="in_progress", conclusion=None)
    next(job for job in jobs if job["name"] == "unit-lost")["conclusion"] = "success"
    assert ci_gate_queue.verify(jobs, [2, 7]) is False


def test_a_pending_recovery_matrix_placeholder_waits_for_expansion():
    jobs = complete()
    next(job for job in jobs if job["name"] == "unit (3.12, 7)")["conclusion"] = "failure"
    next(job for job in jobs if job["name"] == "unit-lost")["conclusion"] = "success"
    next(job for job in jobs if job["name"].startswith("unit rerun")).update(status="queued", conclusion=None)
    assert ci_gate_queue.verify(jobs, [2, 7], lost=lambda job: True) is False


def test_main_binds_the_current_queue_attempt_and_exact_split_plan(monkeypatch):
    observed = []
    values = {
        "GITHUB_EVENT_NAME": "merge_group",
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "4",
        "GITHUB_REF": "refs/queue",
        "GITHUB_SHA": "head",
        "QUEUE_PLAN": '[{"shard": 2}, {"shard": 7}]',
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(ci_gate_queue, "watch", lambda *args: observed.append(args), raising=False)
    ci_gate_queue.main()
    assert observed == [("owner/repo", "123", "4", [2, 7], "refs/queue", "head")]
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    with pytest.raises(ValueError, match="merge_group"):
        ci_gate_queue.main()


def test_watch_checks_queue_identity_again_after_every_gate_passed(monkeypatch):
    snapshots = iter([[[{"ref": "refs/queue", "object": {"sha": "head"}}]], []])
    monkeypatch.setattr(ci_gate_queue, "api", lambda endpoint: next(snapshots), raising=False)
    monkeypatch.setattr(ci_gate_queue, "read_jobs", lambda endpoint: complete(), raising=False)
    with pytest.raises(ValueError, match=re.compile("queue group", re.I)):
        ci_gate_queue.watch("owner/repo", "123", "2", [2, 7], "refs/queue", "head")


def test_reuse_resolves_the_watcher_nonqueue_name_as_well_as_the_required_context():
    from scripts import ci_reuse

    workflow = {
        "jobs": {
            "gate-required": {"name": ci_gate_queue.SUMMARY_NAME, "needs": ["unit", "queue-fail-fast"]},
            "unit": {},
            "queue-fail-fast": {"name": ci_gate_queue.GATE_NAME},
        }
    }
    jobs = [{"name": name, "conclusion": "success"} for name in ["Gate — Required", "unit", "queue-fail-fast"]]
    assert ci_reuse._passed(workflow, jobs)


@pytest.mark.parametrize("shards", [[], [2, 2], [True]])
def test_watch_rejects_an_invalid_plan_before_any_api_read(monkeypatch, shards):
    monkeypatch.setattr(ci_gate_queue, "api", lambda endpoint: (_ for _ in ()).throw(AssertionError("must not read")))
    with pytest.raises(ValueError):
        ci_gate_queue.watch("owner/repo", "123", "2", shards, "refs/queue", "head")


def paired():
    from pathlib import Path

    import yaml

    workflow = yaml.safe_load((Path(__file__).resolve().parents[1] / ".github/workflows/test.yml").read_text())
    jobs = workflow["jobs"]
    jobs["gate-required"]["name"] = ci_gate_queue.SUMMARY_NAME
    jobs["queue-fail-fast"] = ci_gate_queue.watcher(jobs["gate-required"]["runs-on"], parallel="queue-split" in jobs)
    return {"test.yml": workflow}


def test_a_positive_first_shard_and_ancillary_jobs_do_not_hide_inventory():
    jobs = complete()
    next(job for job in jobs if job["name"] == "unit (3.12, 2)")["name"] = "unit (3.12, 1)"
    ancillary = [{"name": "Gate — Required", "status": "in_progress"}, {"name": "test-summary", "status": "queued"}]
    assert ci_gate_queue.verify(ancillary + jobs, [1, 7]) is True


@pytest.mark.parametrize("name", ["unit (3.12, 2) (extra)", "unit rerun (3.12, 3)"])
def test_malformed_or_unplanned_jobs_are_rejected_before_missing_originals_finish(name):
    jobs = [job for job in complete() if job["name"] != "unit (3.12, 7)"]
    jobs.append({"name": name, "status": "completed", "conclusion": "success"})
    with pytest.raises(ValueError) as failure:
        ci_gate_queue.verify(jobs, [2, 7])
    assert name in str(failure.value)


def test_another_interpreter_cannot_replace_the_required_shard_checker():
    jobs = complete()
    next(job for job in jobs if job["name"] == "shard-check (3.12)")["name"] = "shard-check (3.11)"
    with pytest.raises(ValueError, match=re.escape("shard-check (3.11)")):
        ci_gate_queue.verify(jobs, [2, 7])


def test_recovery_placeholders_and_unneeded_reruns_report_their_rejection():
    jobs = complete()
    jobs.append({"name": "unit rerun (3.12, 2)", "status": "completed", "conclusion": "success"})
    with pytest.raises(ValueError, match=re.compile("lost original", re.I)):
        ci_gate_queue.verify(jobs, [2, 7])
    jobs = complete()
    next(job for job in jobs if job["name"] == "unit (3.12, 7)")["conclusion"] = "failure"
    next(job for job in jobs if job["name"] == "unit-lost")["conclusion"] = "success"
    with pytest.raises(ValueError, match=re.compile("matrix.*lost original", re.I)):
        ci_gate_queue.verify(jobs, [2, 7], lost=lambda job: job["name"] == "unit (3.12, 7)")


def test_a_recovery_watch_uses_the_exact_job_repository_and_shared_loss_cache(monkeypatch):
    jobs = complete()
    original = next(job for job in jobs if job["name"] == "unit (3.12, 7)")
    original["conclusion"] = "failure"
    next(job for job in jobs if job["name"] == "unit-lost")["conclusion"] = "success"
    jobs = [job for job in jobs if not job["name"].startswith("unit rerun")]
    jobs.append({"id": 100, "name": "unit rerun (3.12, 7)", "status": "completed", "conclusion": "success"})
    snapshots = iter([[job for job in jobs if job["name"] != "shard-check (3.12)"], jobs])
    caches = []

    def loss(repository, job, seen):
        assert repository == "owner/repo"
        assert job is original
        assert isinstance(seen, dict)
        if caches:
            assert seen is caches[0]
            assert seen[original["id"]] is True
        seen[original["id"]] = True
        caches.append(seen)
        return True

    monkeypatch.setattr(ci_gate_queue, "read_jobs", lambda endpoint: next(snapshots))
    monkeypatch.setattr(ci_gate_queue, "api", lambda endpoint: [[{"ref": "refs/queue", "object": {"sha": "head"}}]])
    monkeypatch.setattr(ci_gate_queue, "logged_loss", loss)
    monkeypatch.setattr(ci_gate_queue.time, "sleep", lambda seconds: None)
    ci_gate_queue.watch("owner/repo", "123", "2", [2, 7], "refs/queue", "head")
    assert len(caches) == 2


def test_context_normalization_rejects_extra_partial_and_incomplete_forms():
    for change in ["extra", "summary", "missing-summary", "missing-needs"]:
        workflow = paired()
        jobs = workflow["test.yml"]["jobs"]
        if change == "extra":
            jobs["extra"] = {"name": ci_gate_queue.GATE_NAME}
        elif change == "summary":
            jobs["gate-required"]["name"] = "${{ true && 'Gate — Required' || 'other' }}"
        elif change == "missing-summary":
            jobs["extra"] = jobs.pop("gate-required")
        else:
            del jobs["gate-required"]["needs"]
        with pytest.raises(ValueError, match=re.compile("context pair|inventory", re.I)):
            ci_gate_queue.normalize(workflow)


@pytest.mark.parametrize("condition", [None, "true", "github.event_name == 'merge_group'", "missing-producer"])
def test_early_gate_compatibility_preserves_the_existing_producer_exclusion(condition):
    workflow = paired()
    assert ci_gate_queue.normalize(workflow)
    if condition is None:
        del workflow["test.yml"]["jobs"]["reuse"]["if"]
    elif condition == "missing-producer":
        del workflow["test.yml"]["jobs"]["reuse"]
    else:
        workflow["test.yml"]["jobs"]["reuse"]["if"] = condition
    with pytest.raises(ValueError, match=re.compile("reuse", re.I)):
        ci_gate_queue.normalize(workflow)


def test_a_stale_queue_group_emits_the_annotation_used_to_count_rebuilds(monkeypatch, capsys):
    monkeypatch.setattr(ci_gate_queue, "api", lambda endpoint: [])
    with pytest.raises(ValueError):
        ci_gate_queue.current("owner/repo", "refs/queue", "head")
    assert capsys.readouterr().out == "::error::queue group is no longer current\n"


def test_paired_names_and_api_waits_remain_exact_dependencies_of_originals_and_reruns():
    from scripts import ci_lost_runner, ci_queue_retry

    workflow = {
        "jobs": {
            "unit": {},
            "unit-rerun": {},
            "split": {},
            "shard-check": {"needs": ["split"], "steps": [{"run": "python -m scripts.ci_gate_shards"}]},
            "gate-required": {"name": ci_gate_queue.SUMMARY_NAME, "needs": ["shard-check", "queue-fail-fast"]},
            "queue-fail-fast": {
                "name": ci_gate_queue.GATE_NAME,
                "needs": ["split"],
                "steps": [{"run": "python3 -m scripts.ci_gate_queue"}],
            },
            "ordinary": {"steps": [{}]},
        }
    }
    names = ci_queue_retry.job_names(workflow)
    assert names == {
        "unit": "unit",
        "unit-rerun": "unit-rerun",
        "split": "split",
        "shard-check": "shard-check",
        "gate-required": "Queue gate summary",
        "queue-fail-fast": "Gate — Required",
        "ordinary": "ordinary",
    }
    assert ci_lost_runner.job_ids(workflow)["Gate — Required"] == "queue-fail-fast"
    assert ci_queue_retry.polling_jobs(workflow) == {"shard-check", "Gate — Required"}
    for root in ["unit", "unit-rerun"]:
        assert ci_queue_retry.dependents(workflow, root) == {"shard-check", "Gate — Required", "Queue gate summary"}
    for missing in ["gate-required", "queue-fail-fast"]:
        changed = copy.deepcopy(workflow)
        changed["jobs"][missing]["name"] = "other"
        expected = {key: value.get("name", key) for key, value in changed["jobs"].items()}
        assert ci_queue_retry.job_names(changed) == expected


def test_reuse_keeps_a_legacy_custom_watcher_display_name():
    from scripts import ci_reuse

    workflow = {
        "jobs": {
            "gate-required": {"name": "Gate — Required", "needs": ["queue-fail-fast"]},
            "queue-fail-fast": {"name": "custom watcher"},
        }
    }
    jobs = [{"name": name, "conclusion": "success"} for name in ["Gate — Required", "custom watcher"]]
    assert ci_reuse._passed(workflow, jobs)
