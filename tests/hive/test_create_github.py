import json
import subprocess

import pytest

from scripts.hive import auth, create_github

pytestmark = pytest.mark.unit

PR = "https://github.com/the-cloud-clockwork/antoncore/pull/7"
REQUIRED = ("Gate — Required",)


def _run(rollup, required=()):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        answer = list(required) if argv[1] == "api" else {"statusCheckRollup": rollup}
        return subprocess.CompletedProcess(argv, 0, json.dumps(answer) + "\n", "")

    run.calls = calls
    return run


def _check(conclusion, status="COMPLETED", name="Gate"):
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion}


@pytest.mark.parametrize("conclusion", ["SUCCESS", "NEUTRAL", "SKIPPED"])
def test_completed_passing_checks_pass(conclusion):
    run = _run([_check("SUCCESS", name="Detect"), _check(conclusion)])
    assert create_github.checks_passed(run, PR) is True
    assert run.calls == [["gh", "pr", "view", PR, "--json", "statusCheckRollup"]]


def test_required_checks_are_read_from_the_base_branch_rules():
    run = _run([], required=["Gate — Required"])
    assert create_github.required_checks(run, "the-cloud-clockwork/antoncore", "dev") == ["Gate — Required"]
    assert run.calls == [
        ["gh", "api", "repos/the-cloud-clockwork/antoncore/rules/branches/dev", "--jq", create_github.REQUIRED]
    ]


def test_only_required_checks_are_graded_when_the_base_requires_some():
    rollup = [_check("SUCCESS", name="Gate — Required"), _check("FAILURE", name="Gate — Protected")]
    assert create_github.checks_passed(_run(rollup), PR, REQUIRED) is True


def test_a_failed_required_check_refuses():
    rollup = [_check("FAILURE", name="Gate — Required"), _check("SUCCESS", name="Detect")]
    with pytest.raises(auth.HiveError) as raised:
        create_github.checks_passed(_run(rollup), PR, REQUIRED)
    assert str(raised.value) == f"pull request {PR} has failed checks: Gate — Required; fix them and rerun hive create"


def test_a_required_check_not_yet_reported_has_not_passed():
    rollup = [_check("SUCCESS", name="Detect"), _check("FAILURE", name="Gate — Protected")]
    assert create_github.checks_passed(_run(rollup), PR, REQUIRED) is False


@pytest.mark.parametrize("order", [1, -1])
def test_a_rerun_required_check_that_passed_passes_in_either_order(order):
    rollup = [_check("FAILURE", name="Gate — Required"), _check("SUCCESS", name="Gate — Required")][::order]
    assert create_github.checks_passed(_run(rollup), PR, REQUIRED) is True


@pytest.mark.parametrize("order", [1, -1])
def test_a_rerun_required_check_still_running_waits_in_either_order(order):
    rollup = [_check("FAILURE", name="Gate — Required"), _check("", status="IN_PROGRESS", name="Gate — Required")]
    assert create_github.checks_passed(_run(rollup[::order]), PR, REQUIRED) is False


@pytest.mark.parametrize(
    "rollup",
    [
        [],
        None,
        [_check("SUCCESS"), _check("", status="IN_PROGRESS")],
        [{"__typename": "StatusContext", "context": "Gate", "state": "PENDING"}],
        [{"__typename": "StatusContext", "context": "Gate", "state": "EXPECTED"}],
    ],
)
def test_missing_or_running_checks_have_not_passed(rollup):
    assert create_github.checks_passed(_run(rollup), PR) is False


def test_failed_checks_are_named_from_check_runs_and_status_contexts():
    rollup = [
        _check("SUCCESS", name="Detect"),
        _check("TIMED_OUT", name="Render"),
        {"__typename": "StatusContext", "context": "Gate — Required", "state": "ERROR"},
    ]
    with pytest.raises(auth.HiveError) as raised:
        create_github.checks_passed(_run(rollup), PR)
    assert str(raised.value) == (
        f"pull request {PR} has failed checks: Render, Gate — Required; fix them and rerun hive create"
    )


def test_merge_answers_whether_auto_merge_was_accepted():
    accepted = _run([])
    assert create_github.merge(accepted, PR) is True
    assert accepted.calls == [["gh", "pr", "merge", PR, "--squash", "--auto"]]

    def refused(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "GraphQL: Auto merge is not allowed for this repository")

    assert create_github.merge(refused, PR) is False


def test_squash_merges_without_auto():
    run = _run([])
    create_github.squash(run, PR)
    assert run.calls == [["gh", "pr", "merge", PR, "--squash"]]


def _tree_run(answer):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, answer, "")

    run.calls = calls
    return run


def test_tree_lists_every_path_of_the_base():
    run = _tree_run("truncated.md\nk8s/a.yaml\nk8s/b/c.yaml\n")
    assert create_github.tree(run, "o/r", "dev") == {"truncated.md", "k8s/a.yaml", "k8s/b/c.yaml"}
    assert run.calls == [["gh", "api", "repos/o/r/git/trees/dev?recursive=1", "--jq", create_github.TREE]]
    assert create_github.TREE == 'if .truncated then "truncated" else .tree[].path end'


def test_a_truncated_tree_is_a_refusal():
    with pytest.raises(auth.HiveError) as refused:
        create_github.tree(_tree_run("truncated\n"), "o/r", "dev")
    assert str(refused.value) == "the o/r dev tree is too large to list, so hive create cannot see its hive files"
