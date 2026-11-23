from pathlib import Path

import pytest
import yaml

from scripts import gate_protected
from tests.test_gate_protected import _ROOT, _commit, _git, _workflow
from tests.test_gate_protected import repo as repo

pytestmark = pytest.mark.unit

RUNNER = "    runs-on: ubuntu-latest\n"


def _plant(repo: Path, text: str, message: str) -> str:
    (repo / ".github/workflows/test.yml").write_text(text)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", message)
    return _git(repo, "rev-parse", "HEAD")


def _grade_head(repo: Path, text: str) -> int:
    _commit(repo, {"test.yml": _workflow()}, {}, "base")
    _git(repo, "checkout", "-q", "-b", "pr")
    head = _plant(repo, text, "plant the head")
    _git(repo, "checkout", "-q", "dev")
    return gate_protected.main(["--root", str(repo), "--head", head])


def _refusal(line: int, reason: str) -> str:
    return f"::error::.github/workflows/test.yml line {line}: {reason}, so the head is refused.\n"


@pytest.mark.feature("protected-grader-refuses-repeated-and-merge-keys")
def test_a_head_repeating_jobs_is_refused_though_the_default_loader_reads_only_the_last_copy(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dropped = _workflow()
    dropped["jobs"]["gate"]["needs"].remove("lint")
    clean = yaml.safe_dump(_workflow(), allow_unicode=True)
    text = yaml.safe_dump({"jobs": dropped["jobs"]}, allow_unicode=True) + clean
    assert yaml.safe_load(text) == _workflow()
    assert _grade_head(repo, text) == 1
    line = [number for number, row in enumerate(text.splitlines(), 1) if row == "jobs:"][1]
    assert capsys.readouterr().out == _refusal(line, "the key jobs repeats an earlier key of its mapping")


@pytest.mark.feature("protected-grader-refuses-repeated-and-merge-keys")
def test_a_head_merging_a_job_is_refused_though_the_default_loader_reads_the_base_job(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = yaml.safe_dump(_workflow(), allow_unicode=True).replace(f"  lint:\n{RUNNER}", "")
    text = text.replace("  unit:\n", "  unit: &runner\n") + "  lint:\n    <<: *runner\n"
    assert yaml.safe_load(text) == _workflow()
    assert _grade_head(repo, text) == 1
    assert capsys.readouterr().out == _refusal(len(text.splitlines()), "GitHub Actions does not merge the key <<")


@pytest.mark.feature("protected-grader-refuses-repeated-and-merge-keys")
def test_a_head_repeating_a_job_in_another_letter_case_is_refused(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    head = _workflow()
    for job in ("AUDIT", "audit"):
        head["jobs"][job] = {"runs-on": "ubuntu-latest"}
        head["jobs"]["gate"]["needs"].append(job)
    text = yaml.safe_dump(head, allow_unicode=True)
    assert _grade_head(repo, text) == 1
    line = text.splitlines().index("  audit:") + 1
    assert capsys.readouterr().out == _refusal(line, "the key audit repeats an earlier key of its mapping")


@pytest.mark.feature("protected-grader-refuses-repeated-and-merge-keys")
def test_a_head_naming_two_jobs_the_default_loader_reads_as_one_boolean_is_refused(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    head = _workflow()
    head["jobs"]["gate"]["needs"] += ["on", "yes"]
    text = yaml.safe_dump(head, allow_unicode=True) + f"  on:\n{RUNNER}  yes:\n{RUNNER}"
    assert sorted(map(str, yaml.safe_load(text)["jobs"])) == ["True", "gate", "lint", "unit", "wiring"]
    assert _grade_head(repo, text) == 1
    line = text.splitlines().index("  yes:") + 1
    assert capsys.readouterr().out == _refusal(line, "the key yes repeats an earlier key of its mapping")


@pytest.mark.feature("protected-grader-refuses-repeated-and-merge-keys")
def test_a_base_repeating_jobs_still_grades_the_head_that_removes_the_repeat(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit(repo, {"test.yml": _workflow()}, {}, "base")
    _plant(repo, "jobs: {}\n" + yaml.safe_dump(_workflow(), allow_unicode=True), "dev repeats jobs")
    _git(repo, "checkout", "-q", "-b", "pr")
    clean = _commit(repo, {"test.yml": _workflow()}, {}, "remove the repeated jobs")
    _git(repo, "checkout", "-q", "dev")
    assert gate_protected.main(["--root", str(repo), "--head", clean]) == 0
    assert capsys.readouterr().out.endswith(": 0 protected gate problems\n")


@pytest.mark.feature("protected-grader-refuses-repeated-and-merge-keys")
def test_the_repository_workflows_pass_the_strict_head_read() -> None:
    root = Path(_git(_ROOT, "rev-parse", "--show-toplevel"))
    assert "test.yml" in gate_protected.load(root, "HEAD", gate_protected.ActionsLoader)[0]
    assert gate_protected.load(root, "HEAD", gate_protected.ActionsLoader) == gate_protected.load(root, "HEAD")
