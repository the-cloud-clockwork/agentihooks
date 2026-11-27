"""The issues a pull request body closes that no task it settles records or names."""

from typing import Any, NoReturn

import pytest

from scripts.swarm import linked_issues
from scripts.swarm.store import SwarmError

URL = "https://github.com/o/r/pull/3"
REPO = "https://github.com/o/r"


@pytest.mark.feature("swarm-pr-closes-only-named-issues")
@pytest.mark.parametrize(
    ("body", "tasks", "unnamed"),
    [
        ("Closes #9", [{"issue_url": f"{REPO}/issues/9"}], []),
        ("Fixes #9", [{"description": "Fix the crash reported in #9"}], []),
        ("Resolves #9", [{"description": f"See {REPO}/issues/9 for the crash"}], []),
        ("Closes #9", [{"issue_url": f"{REPO}/issues/5"}, {"issue_url": f"{REPO}/issues/9"}], []),
        ("Refs #9", [{}], []),
        ("Closes #9", [{"issue_url": f"{REPO}/issues/5", "description": "Fix #99 and #90"}], [f"{REPO}/issues/9"]),
        ("Closes #9", [{"issue_url": "https://github.com/o/other/issues/9"}], [f"{REPO}/issues/9"]),
        ("Closes #9", [{"issue_url": "https://githubXcom/o/r/issues/9"}], [f"{REPO}/issues/9"]),
        ("Closes #5. Fixes #9, fixes #9", [{"issue_url": f"{REPO}/issues/5"}], [f"{REPO}/issues/9"]),
    ],
)
def test_unnamed_lists_each_closed_issue_no_task_records_or_names(
    body: str, tasks: list[dict[str, Any]], unnamed: list[str]
) -> None:
    assert linked_issues.unnamed(URL, body, tasks) == unnamed


def _no_task(task_id: str) -> NoReturn:
    raise AssertionError(f"read {task_id} for a task with no group")


@pytest.mark.feature("swarm-pr-closes-only-named-issues")
def test_refuse_unnamed_names_every_issue_the_tasks_never_name() -> None:
    with pytest.raises(SwarmError) as refused:
        linked_issues.refuse_unnamed(URL, "Closes #7 and closes #9", {"issue_url": f"{REPO}/issues/5"}, _no_task)
    assert str(refused.value) == (
        f"pull request {URL} closes {REPO}/issues/7, {REPO}/issues/9, which this task neither records nor names in "
        "its description: drop the closing keyword from the body, or record the task's own issue with swarm issue"
    )


@pytest.mark.feature("swarm-pr-closes-only-named-issues")
def test_refuse_unnamed_accepts_a_body_closing_only_named_issues() -> None:
    assert linked_issues.refuse_unnamed(URL, "Closes #5", {"issue_url": f"{REPO}/issues/5"}, _no_task) is None


@pytest.mark.feature("swarm-pr-closes-only-named-issues")
def test_refuse_unnamed_reads_each_group_member_issue_and_description() -> None:
    members = {"t2": {"issue_url": f"{REPO}/issues/7"}, "t3": {"description": "Also fixes #9"}, "t4": None}
    row = {"issue_url": f"{REPO}/issues/5", "group_members": ["t2", "t3", "t4"]}
    assert linked_issues.refuse_unnamed(URL, "Closes #5, closes #7, closes #9", row, members.__getitem__) is None
    with pytest.raises(SwarmError) as refused:
        linked_issues.refuse_unnamed(URL, "Closes #8", row, members.__getitem__)
    assert str(refused.value) == (
        f"pull request {URL} closes {REPO}/issues/8, which this task neither records nor names in its description: "
        "drop the closing keyword from the body, or record the task's own issue with swarm issue"
    )
