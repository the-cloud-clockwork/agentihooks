import json
import subprocess

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
HOTFIX = [{"number": 9, "merged_at": "2026-11-11T10:00:00Z", "labels": [{"name": "launch hotfix"}]}]


def gh(pulls: list[dict], calls: list | None = None, code: int = 0):
    def run(argv, **kwargs):
        if calls is not None:
            calls.append((argv, kwargs))
        if code:
            raise subprocess.CalledProcessError(code, argv, "", "boom")
        return subprocess.CompletedProcess(argv, 0, json.dumps(pulls), "")

    return run


def promote(needs: dict, run, capsys) -> str:
    environ = {"NEEDS": json.dumps(needs)}
    assert ci_launch_hotfix.main(["--repo", "o/r", "--sha", "abc"], run, environ) == 0
    return capsys.readouterr().out


@pytest.mark.feature("launch-hotfix-promotion")
def test_a_labelled_merged_pull_with_a_green_unit_suite_promotes(capsys):
    calls = []
    assert promote(GREEN, gh(HOTFIX, calls), capsys) == "promote=true\n"
    assert calls == [
        (["gh", "api", "repos/o/r/commits/abc/pulls"], {"capture_output": True, "text": True, "check": True})
    ]


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
def test_a_pull_without_the_merged_launch_hotfix_label_never_promotes(capsys, pulls):
    assert promote(GREEN, gh(pulls), capsys) == "promote=false\n"


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
    ],
)
def test_a_red_unit_suite_never_promotes_a_labelled_pull(capsys, job, result):
    needs = {**GREEN, job: {"result": result}}
    assert promote(needs, gh(HOTFIX), capsys) == "promote=false\n"


@pytest.mark.feature("launch-hotfix-promotion")
@pytest.mark.parametrize("unit", ["failure", "cancelled"])
def test_a_unit_shard_recovered_by_its_rerun_counts_as_a_passed_suite(capsys, unit):
    needs = {**GREEN, "unit": {"result": unit}, "unit-lost": {"result": "success"}, "unit-rerun": {"result": "success"}}
    assert promote(needs, gh(HOTFIX), capsys) == "promote=true\n"


@pytest.mark.feature("launch-hotfix-promotion")
def test_a_red_unit_suite_skips_the_label_read(capsys):
    calls = []
    assert promote({**GREEN, "unit": {"result": "failure"}}, gh(HOTFIX, calls), capsys) == "promote=false\n"
    assert calls == []


@pytest.mark.feature("launch-hotfix-promotion")
def test_an_unreadable_label_falls_back_to_the_full_grade(capsys):
    environ = {"NEEDS": json.dumps(GREEN)}
    assert ci_launch_hotfix.main(["--repo", "o/r", "--sha", "abc"], gh(HOTFIX, code=1), environ) == 0
    captured = capsys.readouterr()
    assert captured.out == "promote=false\n"
    assert captured.err == "launch hotfix label unreadable, promoting on the full grade only: boom\n"


@pytest.mark.feature("launch-hotfix-promotion")
def test_missing_needs_never_promote(capsys):
    assert ci_launch_hotfix.main(["--repo", "o/r", "--sha", "abc"], gh(HOTFIX), {}) == 0
    assert capsys.readouterr().out == "promote=false\n"
