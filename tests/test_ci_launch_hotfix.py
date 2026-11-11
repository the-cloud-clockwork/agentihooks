import json
import subprocess
from collections.abc import Callable
from typing import Any

import pytest

from scripts import ci_launch_hotfix

pytestmark = pytest.mark.unit

GREEN = {
    "select": {"result": "success"},
    "split": {"result": "success"},
    "unit": {"result": "success"},
    "unit-lost": {"result": "skipped"},
    "unit-rerun": {"result": "skipped"},
    "shard-check": {"result": "success"},
    "test-count": {"result": "success"},
}
HOTFIX = [
    {"number": 9, "merged_at": "2026-11-11T10:00:00Z", "labels": [{"name": "launch hotfix"}], "head": {"sha": "h9"}}
]
PULLS = "repos/o/r/commits/abc/pulls"
CHECKS = "repos/o/r/commits/h9/check-runs?check_name=Gate%20%E2%80%94%20Required"


Runner = Callable[..., subprocess.CompletedProcess[str]]


def gh(
    pulls: list[dict[str, Any]],
    calls: list[tuple[list[str], dict[str, Any]]] | None = None,
    code: int = 0,
    conclusions: str = "success\n",
) -> Runner:
    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if calls is not None:
            calls.append((argv, kwargs))
        if code:
            raise subprocess.CalledProcessError(code, argv, "", "boom")
        out = json.dumps(pulls) if argv[2] == PULLS else conclusions
        return subprocess.CompletedProcess(argv, 0, out, "")

    return run


def promote(needs: dict[str, Any], run: Runner, capsys: pytest.CaptureFixture[str]) -> str:
    environ = {"NEEDS": json.dumps(needs)}
    assert ci_launch_hotfix.main(["--repo", "o/r", "--sha", "abc"], run, environ) == 0
    return capsys.readouterr().out


@pytest.mark.feature("launch-hotfix-promotion")
def test_a_labelled_merged_pull_with_a_green_head_and_unit_suite_promotes(capsys: pytest.CaptureFixture[str]) -> None:
    calls = []
    assert promote(GREEN, gh(HOTFIX, calls), capsys) == "promote=true\n"
    kwargs = {"capture_output": True, "text": True, "check": True}
    assert calls == [
        (["gh", "api", PULLS], kwargs),
        (["gh", "api", CHECKS, "--jq", ".check_runs[].conclusion"], kwargs),
    ]


@pytest.mark.feature("launch-hotfix-promotion")
@pytest.mark.parametrize("conclusions", ["", "failure\n", "cancelled\nneutral\n"])
def test_a_labelled_pull_whose_own_grade_did_not_pass_never_promotes(
    capsys: pytest.CaptureFixture[str], conclusions: str
) -> None:
    assert promote(GREEN, gh(HOTFIX, conclusions=conclusions), capsys) == "promote=false\n"


@pytest.mark.feature("launch-hotfix-promotion")
@pytest.mark.parametrize(
    "pulls",
    [
        [],
        [{**HOTFIX[0], "labels": [{"name": "bug"}]}],
        [{**HOTFIX[0], "merged_at": None}],
    ],
    ids=["no pull", "other label", "unmerged"],
)
def test_a_pull_without_the_merged_launch_hotfix_label_never_promotes(
    capsys: pytest.CaptureFixture[str], pulls: list[dict[str, Any]]
) -> None:
    calls = []
    assert promote(GREEN, gh(pulls, calls), capsys) == "promote=false\n"
    assert [argv[2] for argv, _ in calls] == [PULLS]


@pytest.mark.feature("launch-hotfix-promotion")
@pytest.mark.parametrize(
    "job, result",
    [
        ("select", "failure"),
        ("split", "skipped"),
        ("shard-check", "failure"),
        ("test-count", "cancelled"),
        ("unit", "failure"),
        ("unit-lost", "success"),
        ("unit-rerun", "success"),
    ],
)
def test_a_red_unit_suite_never_promotes_a_labelled_pull(
    capsys: pytest.CaptureFixture[str], job: str, result: str
) -> None:
    needs = {**GREEN, job: {"result": result}}
    assert promote(needs, gh(HOTFIX), capsys) == "promote=false\n"


@pytest.mark.feature("launch-hotfix-promotion")
def test_a_rerun_shard_never_turns_a_red_unit_suite_green(capsys: pytest.CaptureFixture[str]) -> None:
    rerun = {"unit": {"result": "failure"}, "unit-lost": {"result": "success"}, "unit-rerun": {"result": "success"}}
    assert promote({**GREEN, **rerun}, gh(HOTFIX), capsys) == "promote=false\n"


@pytest.mark.feature("launch-hotfix-promotion")
def test_a_red_unit_suite_skips_the_label_read(capsys: pytest.CaptureFixture[str]) -> None:
    calls = []
    assert promote({**GREEN, "unit": {"result": "failure"}}, gh(HOTFIX, calls), capsys) == "promote=false\n"
    assert calls == []


@pytest.mark.feature("launch-hotfix-promotion")
def test_an_unreadable_label_falls_back_to_the_full_grade(capsys: pytest.CaptureFixture[str]) -> None:
    environ = {"NEEDS": json.dumps(GREEN)}
    assert ci_launch_hotfix.main(["--repo", "o/r", "--sha", "abc"], gh(HOTFIX, code=1), environ) == 0
    captured = capsys.readouterr()
    assert captured.out == "promote=false\n"
    assert captured.err == "launch hotfix label unreadable, promoting on the full grade only: boom\n"


@pytest.mark.feature("launch-hotfix-promotion")
def test_missing_needs_never_promote(capsys: pytest.CaptureFixture[str]) -> None:
    assert ci_launch_hotfix.main(["--repo", "o/r", "--sha", "abc"], gh(HOTFIX), {}) == 0
    assert capsys.readouterr().out == "promote=false\n"
