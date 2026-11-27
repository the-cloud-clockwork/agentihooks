"""The identity of a grade includes the test merge GitHub built for a pull request once resolved, issue 13890: a
rerequest after dev moved and a newer merge resolved grades under a new key, and a grade with no resolved merge
keeps the key it always had.
"""

import pytest

from scripts.ci_lab.digests import SCHEMAS, GradeInputs, digest, grade_key

REPO = "the-cloud-clockwork/agentihooks"
HEAD = "a" * 40
BASE = "b" * 40
GRADER = "c" * 40
MERGE = "m" * 40
OTHER_MERGE = "n" * 40


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_grade_key_with_no_graded_commit_matches_the_key_stored_before_this_change() -> None:
    inputs = GradeInputs(REPO, HEAD, BASE, "pull_request", GRADER)
    expected = {"repository": REPO, "head": HEAD, "base": BASE, "tier": "pull_request", "grader": GRADER}
    assert (
        grade_key(inputs)
        == f"grade-v{SCHEMAS['grade']}-{digest({'schema': SCHEMAS['grade'], 'kind': 'grade', 'inputs': expected})}"
    )


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_grade_key_with_a_graded_commit_differs_from_one_without() -> None:
    without = GradeInputs(REPO, HEAD, BASE, "pull_request", GRADER)
    with_merge = GradeInputs(REPO, HEAD, BASE, "pull_request", GRADER, MERGE)
    assert grade_key(without) != grade_key(with_merge)


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_grade_key_changes_when_a_newer_merge_resolves() -> None:
    first = GradeInputs(REPO, HEAD, BASE, "pull_request", GRADER, MERGE)
    second = GradeInputs(REPO, HEAD, BASE, "pull_request", GRADER, OTHER_MERGE)
    assert grade_key(first) != grade_key(second)


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_grade_key_with_an_empty_graded_commit_equals_one_built_with_the_field_left_out() -> None:
    explicit = GradeInputs(REPO, HEAD, BASE, "pull_request", GRADER, "")
    left_out = GradeInputs(REPO, HEAD, BASE, "pull_request", GRADER)
    assert grade_key(explicit) == grade_key(left_out)


def test_a_grade_key_with_a_graded_commit_matches_the_key_built_from_the_graded_commit_field() -> None:
    inputs = GradeInputs(REPO, HEAD, BASE, "pull_request", GRADER, MERGE)
    expected = {
        "repository": REPO,
        "head": HEAD,
        "base": BASE,
        "tier": "pull_request",
        "grader": GRADER,
        "graded_commit": MERGE,
    }
    assert (
        grade_key(inputs)
        == f"grade-v{SCHEMAS['grade']}-{digest({'schema': SCHEMAS['grade'], 'kind': 'grade', 'inputs': expected})}"
    )
