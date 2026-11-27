"""The review pass tick step registers with its group, lane, feature and ports, and names the change it reviews."""

import hashlib
from importlib import import_module
from typing import Any

import pytest

from scripts.tick import registry

REPO = "https://github.com/acme/widgets"
PULL = f"{REPO}/pull/7"
LAB = "https://gitlab.example.com/acme/tools/widgets"
MERGE = f"{LAB}/-/merge_requests/7"
NO_AUTHOR = " No swarm task carries this change."


def review() -> Any:
    return import_module("scripts.tick.steps.review")


def doc(*tasks: dict[str, Any]) -> dict[str, Any]:
    return {"tasks": list(tasks), "phases": []}


def expected(short: str, ident: str, title: str, phase: str, description: str) -> dict[str, Any]:
    key = hashlib.sha256(ident.lower().encode()).hexdigest()[:12]
    return {
        "task": f"review-{short}-{key}",
        "title": title,
        "lane": "review",
        "kind": "review",
        "phase": phase,
        "description": description,
    }


@pytest.mark.feature("swarm-review-trigger")
def test_review_pass() -> None:
    """The review pass step registers in group wrapper phases on the tick lane with its feature and ports."""
    import_module("scripts.tick.steps.review")
    found = {step.name: step for step in registry.steps()}["review pass"]
    assert (found.group, found.lane, found.feature, found.effects) == (
        "wrapper phases",
        "tick",
        "swarm-review-trigger",
        frozenset({"ledger"}),
    )


@pytest.mark.feature("swarm-review-trigger")
def test_a_github_pull_review_keeps_its_title_and_description_with_an_author_task() -> None:
    """A GitHub pull request review names the pull request number and repository and its author task."""
    found = review().review_task("pr", {"url": PULL}, doc({"id": "t1", "pr_url": PULL, "phase": "p1"}))
    assert found == expected(
        "pr", PULL, "Review pull request 7 of widgets", "p1", f"Review the pull request {PULL}. Author task t1."
    )


@pytest.mark.feature("swarm-review-trigger")
def test_a_github_pull_review_keeps_its_title_and_description_without_an_author_task() -> None:
    """A GitHub pull request no swarm task carries gets no phase and says no task carries it."""
    found = review().review_task("pr", {"url": PULL}, doc({"id": "t2", "pr_url": "", "phase": "p2"}))
    assert found == expected(
        "pr", PULL, "Review pull request 7 of widgets", "", f"Review the pull request {PULL}.{NO_AUTHOR}"
    )


@pytest.mark.feature("swarm-review-trigger")
@pytest.mark.parametrize(
    ("number", "title", "phase", "by"),
    [
        (7, "Review the merge queue group for pull request 7 of widgets", "p1", " Author task t1."),
        (None, "Review the merge queue group of widgets", "", NO_AUTHOR),
    ],
)
def test_a_merge_queue_group_review_keeps_its_github_title_and_description(
    number: int | None, title: str, phase: str, by: str
) -> None:
    """A merge queue group review names the pull request it tests, when it names one, and its repository."""
    data = {"repository": REPO, "head": "a1", "number": number}
    found = review().review_task("mq", data, doc({"id": "t1", "pr_url": PULL, "phase": "p1"}))
    assert found == expected("mq", f"{REPO}@a1", title, phase, f"Review the merge queue group {REPO}/commit/a1.{by}")


@pytest.mark.feature("swarm-review-trigger")
def test_a_release_review_keeps_its_github_title_and_description() -> None:
    """A release review names its tag and repository and links the release."""
    url = f"{REPO}/releases/tag/v1.2.3"
    data = {"repository": REPO, "tag": "v1.2.3", "url": url}
    found = review().review_task("rel", data, doc({"id": "t1", "pr_url": PULL, "phase": "p1"}))
    assert found == expected(
        "rel", f"{REPO}@v1.2.3", "Review release v1.2.3 of widgets", "", f"Review the release {url}.{NO_AUTHOR}"
    )


@pytest.mark.feature("swarm-review-trigger")
def test_a_pull_link_the_change_link_parser_refuses_keeps_its_path_position_title() -> None:
    """A pull link outside the forms the change link parser reads keeps the title its path positions give."""
    url = "https://github.example.com/acme/widgets/pull/7"
    found = review().review_task("pr", {"url": url}, doc())
    assert found == expected(
        "pr", url, "Review pull request 7 of widgets", "", f"Review the pull request {url}.{NO_AUTHOR}"
    )


@pytest.mark.feature("swarm-review-trigger")
def test_a_gitlab_merge_request_review_names_its_project_number_remote_and_forge() -> None:
    """A GitLab merge request in a nested group names its project and number and carries the link, clone remote and
    forge kind the reviewer passes to forge read."""
    found = review().review_task("pr", {"url": MERGE}, doc({"id": "t2", "pr_url": "", "phase": "p2"}))
    read = f" Read it with number 7, remote {LAB}.git and forge gitlab."
    assert found == expected(
        "pr", MERGE, "Review merge request 7 of widgets", "", f"Review the merge request {MERGE}.{read}{NO_AUTHOR}"
    )


@pytest.mark.feature("swarm-review-trigger")
def test_a_gitlab_merge_request_review_finds_its_author_task_by_its_link_case_folded() -> None:
    """A GitLab merge request review takes the phase of the task whose recorded link matches, ignoring case."""
    author = {"id": "t9", "pr_url": MERGE.replace("widgets", "Widgets"), "phase": "p9"}
    found = review().review_task("pr", {"url": MERGE}, doc(author))
    read = f" Read it with number 7, remote {LAB}.git and forge gitlab."
    assert found == expected(
        "pr",
        MERGE,
        "Review merge request 7 of widgets",
        "p9",
        f"Review the merge request {MERGE}.{read} Author task t9.",
    )
