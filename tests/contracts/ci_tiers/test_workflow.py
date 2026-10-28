import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tests.test_ci_hosted_lanes import _resolve

pytestmark = pytest.mark.unit

ROOT = Path(__file__).parents[3]
DURATION_JOB = (
    "durations"
    if "durations" in yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    else "select"
)
UNIT = {DURATION_JOB, "split", "unit", "shard-check", "test-count", "gate-required"} | (
    {"test-summary"}
    if "test-summary" in yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    else set()
)
QUEUE = UNIT | {"queue-fail-fast"}
PULL_REQUEST = (
    UNIT
    | {"select", "lint", "semgrep", "sonar", "mutation-plan", "mutation-stats", "mutation", "writer-lock"}
    | ({"docs"} if "docs" in yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"] else set())
)
SELECTED = PULL_REQUEST - {"split", "unit", "shard-check"} | {"unit-selected"}
PROMOTION = (PULL_REQUEST - {"mutation-plan", "mutation-stats", "mutation", "lint", "semgrep", "test-count"}) | {
    "queue-fail-fast",
    "reuse",
    "coverage-ratchet",
    "coverage-baseline",
    "promote",
    "refresh-durations",
    "mutation-stats-seed",
    "count-record",
    "flake-budget",
    "swarm-image",
    "worker-image",
    "brain-smoke",
    "writer-lock",
    "ledger-load",
    "hierarchy-cpu",
    "kind-due",
    "helm-kind",
    "stage-budget",
}


def enabled(condition, event, needs, base_ref="dev"):
    if isinstance(condition, bool):
        return condition
    expression = condition.removeprefix("${{").removesuffix("}}").strip()
    expression = expression.replace("&&", " and ").replace("||", " or ").replace("!cancelled()", "not cancelled()")
    expression = expression.replace("!contains", "not contains").replace("needs.*.result", "results")
    expression = re.sub(r"needs\.([a-z]+(?:-[a-z]+)*)", r'needs["\1"]', expression)
    return bool(
        eval(
            expression,
            {"__builtins__": {}},
            {
                "github": SimpleNamespace(event_name=event, base_ref=base_ref, ref="refs/heads/dev"),
                "contains": lambda values, value: value in values,
                "results": [need.result for need in needs.values()],
                "needs": needs,
                "always": lambda: True,
                "cancelled": lambda: False,
            },
        )
    )


def jobs_for(event, base_ref="dev", scope="full"):
    jobs = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    completed = {}
    pending = dict(jobs)
    while pending:
        ready = False
        for name, job in list(pending.items()):
            dependencies = job.get("needs", [])
            if isinstance(dependencies, str):
                dependencies = [dependencies]
            if not set(dependencies) <= completed.keys():
                continue
            needs = {dependency: completed[dependency] for dependency in dependencies}
            condition = job.get("if", "")
            if "if" in job:
                runs = enabled(condition, event, needs, base_ref)
            else:
                runs = all(need.result == "success" for need in needs.values())
            completed[name] = SimpleNamespace(
                result="success" if runs else "skipped",
                outputs=SimpleNamespace(
                    due="true",
                    reused="false",
                    forwarded="false",
                    scope=scope if runs else "",
                    analysis="A1" if runs else "",
                    shards="[0, 1]",
                    stats_parts="[0, 1]",
                    sonar="run",
                ),
            )
            del pending[name]
            ready = True
        assert ready, pending
    return {name for name, job in completed.items() if job.result == "success"}


@pytest.mark.parametrize(
    "event,expected", [("merge_group", QUEUE), ("pull_request", PULL_REQUEST), ("push", PROMOTION)]
)
def test_each_event_runs_only_its_tier_jobs(event, expected):
    if event == "push" and (ROOT / ".github/workflows/count-record.yml").exists():
        expected = expected - {"count-record"}
    if event == "push":
        assert jobs_for(event) in (expected, expected | {"install-smoke"})
    else:
        assert jobs_for(event) == expected


def test_selected_pull_requests_run_the_selected_leg_instead_of_the_shards():
    assert jobs_for("pull_request", scope="selected") == SELECTED


def test_main_pull_requests_keep_their_nonmutation_checks():
    assert jobs_for("pull_request", "main") == PULL_REQUEST - {"mutation-plan", "mutation-stats", "mutation"}


def test_the_retired_queue_baseline_leaves_the_workflow_and_the_gate_takes_the_promotion_form():
    from scripts import ci_reuse

    jobs = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    assert "queue-baseline" not in jobs
    assert not any("queue-baseline" in job.get("needs", []) for job in jobs.values())
    options = {"consolidated": True} if DURATION_JOB == "select" else {}
    steps = jobs["gate-required"]["steps"]
    steps[0]["run"] = steps[0]["run"].replace('"lint", "docs", "semgrep"', '"lint", "semgrep"')
    assert steps in [
        ci_reuse.test_only_sonar(
            ci_reuse.selection_diagnostic(
                forward(
                    finish(
                        rerun(
                            ci_reuse.inline_mutation(
                                ci_reuse.postgres_required(
                                    ci_reuse.gate_steps(
                                        queue_tier=True, promotion=True, **options, summary=summary, watcher_loss=True
                                    )
                                )
                            )
                        )
                    )
                )
            )
        )
        for forward in (list, ci_reuse.forwarded_stats)
        for summary in (False, True)
        for rerun in (list, ci_reuse.lost_rerun)
        for finish in (list, ci_reuse.pipefail_summary)
    ]


def test_the_queue_gate_needs_the_fail_fast_watcher():
    from scripts.ci_queue_fail_fast import SELF

    jobs = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    assert SELF in jobs, f"the Tests workflow has no {SELF} job"
    assert SELF in jobs["gate-required"]["needs"], f"the required gate does not need {SELF}"


def test_the_queue_keeps_completeness_but_skips_the_shard_time_budget():
    job = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]["shard-check"]
    completeness = next(
        step for step in job["steps"] if step.get("name") == "Check every collected test ran in exactly one shard"
    )
    budget = next(step for step in job["steps"] if "tests.shard_budget" in step.get("run", ""))
    assert "merge_group" not in completeness.get("if", "")
    assert enabled(budget.get("if", True), "merge_group", {}) is False
    assert enabled(budget.get("if", True), "pull_request", {}) is True


def test_the_shard_time_budget_grades_pull_requests_and_reports_on_promotion():
    job = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]["shard-check"]
    budget = next(step for step in job["steps"] if "tests.shard_budget" in step.get("run", ""))
    flag = budget["env"]["REPORT"]
    assert _resolve({"group": flag}, event_name="push")["group"] == "--report"
    assert _resolve({"group": flag}, event_name="pull_request")["group"] == ""
    assert re.fullmatch(
        r"python3? -m tests\.shard_budget \$REPORT (shard-durations/\*|shard-reports/durations-\*)/durations\.json",
        budget["run"],
    )


def _concurrency(event, run_id, number=None, ref_name="dev"):
    workflow = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())
    return _resolve(
        workflow["concurrency"],
        event_name=event,
        run_id=run_id,
        ref_name=ref_name,
        event={"pull_request": {"number": number}},
    )


def test_dev_pushes_share_one_group_so_the_running_run_finishes_and_a_newer_push_replaces_the_pending_one():
    first, second = _concurrency("push", 1000), _concurrency("push", 1061)
    expected = {"group": "tests-push-dev", "queue": "single", "cancel-in-progress": False}
    assert (first, second) == (expected, expected)


@pytest.mark.parametrize("event,number", [("merge_group", None), ("pull_request", 7)])
def test_queue_and_pull_request_runs_keep_their_own_groups(event, number):
    first, second = _concurrency(event, 1000, number), _concurrency(event, 1061, number + 1 if number else None)
    assert first["group"] != second["group"]
    assert "promotion" not in first["group"]
    assert first["cancel-in-progress"] is (event == "pull_request")
