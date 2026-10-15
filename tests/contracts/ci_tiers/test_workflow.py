import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).parents[3]
UNIT = {"durations", "split", "unit", "shard-check", "test-count", "gate-required"}
QUEUE = UNIT | {"queue-fail-fast"}
PULL_REQUEST = UNIT | {"select", "lint", "semgrep", "sonar", "mutation-plan", "mutation-stats", "mutation"}
SELECTED = PULL_REQUEST - {"split", "unit", "shard-check"} | {"unit-selected"}


def enabled(condition, event, needs, base_ref="dev"):
    if isinstance(condition, bool):
        return condition
    expression = condition.removeprefix("${{").removesuffix("}}").strip()
    expression = expression.replace("&&", " and ").replace("||", " or ").replace("!cancelled()", "not cancelled()")
    expression = re.sub(r"needs\.([a-z]+(?:-[a-z]+)*)", r'needs["\1"]', expression)
    return bool(
        eval(
            expression,
            {"__builtins__": {}},
            {
                "github": SimpleNamespace(event_name=event, base_ref=base_ref),
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
                outputs=SimpleNamespace(due="true", reused="false", scope=scope if runs else ""),
            )
            del pending[name]
            ready = True
        assert ready, pending
    return {name for name, job in completed.items() if job.result == "success"}


@pytest.mark.parametrize("event,expected", [("merge_group", QUEUE), ("pull_request", PULL_REQUEST)])
def test_each_event_runs_only_its_tier_jobs(event, expected):
    assert jobs_for(event) == expected


def test_selected_pull_requests_run_the_selected_leg_instead_of_the_shards():
    assert jobs_for("pull_request", scope="selected") == SELECTED


def test_main_pull_requests_keep_their_nonmutation_checks():
    assert jobs_for("pull_request", "main") == PULL_REQUEST - {"mutation-plan", "mutation-stats", "mutation"}


def test_the_queue_keeps_completeness_but_skips_the_shard_time_budget():
    job = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]["shard-check"]
    completeness = next(
        step for step in job["steps"] if step.get("name") == "Check every collected test ran in exactly one shard"
    )
    budget = next(step for step in job["steps"] if "tests.shard_budget" in step.get("run", ""))
    assert "merge_group" not in completeness.get("if", "")
    assert enabled(budget.get("if", True), "merge_group", {}) is False
    assert enabled(budget.get("if", True), "pull_request", {}) is True
