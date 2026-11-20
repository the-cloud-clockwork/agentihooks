"""On a pull request into dev every Tests grader and planner reads the event base, the dev commit its merge holds."""

import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.ci_lab import expressions

pytestmark = pytest.mark.unit

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/test.yml"
EVENT_BASE = "event-base"
PULL_REQUEST = {
    "github.base_ref": "dev",
    "github.event.before": "",
    "github.event.merge_group.base_sha": "",
    "github.event.pull_request.base.sha": EVENT_BASE,
    "github.event.pull_request.head.repo.full_name": "o/r",
    "github.event.pull_request.head.sha": "head",
    "github.event_name": "pull_request",
    "github.repository": "o/r",
    "github.sha": "merge",
    "inputs.base": "",
    "matrix.python-version": "3.12",
    "needs.reuse.outputs.grader": "",
}
DEV_TIP = frozenset({"dev", "origin/dev", "refs/heads/dev", "refs/remotes/origin/dev"})
WORKTREE = re.compile(r'git worktree add --detach "\$RUNNER_TEMP/(?:grader|planner)" (\S+)')
FETCH = re.compile(r'git fetch\b[^\n]* origin "\$(\w+)"')
VARIABLE = re.compile(r'"\$(\w+)"')
REUSE_GRADERS = {
    ("test-count", "Plan the tests whose inputs did not change"),
    ("unit", "Keep the reused tests' dev coverage"),
    ("unit-selected", "Adopt the reused tests from the protected grader"),
}


def _may_run(item: Mapping[str, Any]) -> bool:
    """False only when an if condition that reads nothing but pull request context is false on a pull request."""
    condition = str(item.get("if", ""))
    readable = expressions.reads(condition, ()) <= PULL_REQUEST.keys()
    return not readable or expressions.holds(condition, PULL_REQUEST)


def _steps() -> Iterator[tuple[str, dict[str, Any]]]:
    """Each Tests workflow step that may run on a pull request into dev, with its job's name."""
    jobs: dict[str, dict[str, Any]] = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    for job, body in jobs.items():
        if _may_run(body):
            yield from ((job, step) for step in body.get("steps", ()) if _may_run(step))


def _worktrees() -> list[tuple[str, str, str]]:
    """Each grader or planner worktree a pull request run adds: its job, its step and the revision it holds."""
    found: list[tuple[str, str, str]] = []
    for job, step in _steps():
        run = str(step.get("run", ""))
        for added in WORKTREE.finditer(run):
            ref = added.group(1)
            if ref == "FETCH_HEAD":
                ref = f'"${FETCH.findall(run[: added.start()])[-1]}"'
            variable = VARIABLE.fullmatch(ref)
            revision = ref if variable is None else expressions.interpolate(step["env"][variable[1]], PULL_REQUEST)
            found.append((job, step["name"], revision))
    return found


def test_every_pull_request_grader_and_planner_worktree_holds_the_event_base() -> None:
    worktrees = _worktrees()
    assert REUSE_GRADERS <= {(job, step) for job, step, _ in worktrees}
    assert [worktree for worktree in worktrees if worktree[2] != EVENT_BASE] == []


def test_no_pull_request_checkout_of_this_repository_takes_the_dev_tip() -> None:
    checkouts = [
        (job, expressions.interpolate(str(step.get("with", {}).get("ref", "")), PULL_REQUEST))
        for job, step in _steps()
        if str(step.get("uses", "")).startswith("actions/checkout@") and "repository" not in step.get("with", {})
    ]
    assert ("shard-check", EVENT_BASE) in checkouts
    assert [checkout for checkout in checkouts if checkout[1] in DEV_TIP] == []
