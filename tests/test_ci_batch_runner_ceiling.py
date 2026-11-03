import os
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts import ci_batch_runner_ceiling as ceiling
from scripts.ci_mutation import plan

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]


def _agentihooks(tmp_path: Path, body: str) -> Path:
    source = tmp_path / "agentihooks" / ceiling.PLANNER
    source.parent.mkdir(parents=True)
    source.write_text(body)
    return tmp_path / "agentihooks"


def _antoncore(tmp_path: Path, runners: int) -> Path:
    values = tmp_path / "antoncore" / ceiling.BATCH_VALUES
    values.parent.mkdir(parents=True)
    values.write_text(
        yaml.safe_dump({"gha-runner-scale-set": {"runnerScaleSetName": "anton-oss-batch", "maxRunners": runners}})
    )
    return tmp_path / "antoncore"


def _run(capsys: pytest.CaptureFixture[str], agentihooks: Path, antoncore: Path) -> tuple[int, list[str]]:
    status = ceiling.main(["--agentihooks", str(agentihooks), "--antoncore", str(antoncore)])
    return status, capsys.readouterr().out.splitlines()


def test_the_reader_finds_the_planner_constant_in_this_head() -> None:
    assert ceiling.planner_ceiling(ROOT) == plan.BATCH_RUNNERS


def test_equal_ceilings_pass(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    agentihooks = _agentihooks(tmp_path, "WORKERS = 4\nsettings.BATCH_RUNNERS = 9\nBATCH_RUNNERS = 40\n")
    status, lines = _run(capsys, agentihooks, _antoncore(tmp_path, 40))
    assert status == 0
    assert lines == [f"{ceiling.PLANNER} BATCH_RUNNERS and antoncore {ceiling.BATCH_VALUES} maxRunners are both 40."]


@pytest.mark.parametrize("missing", ["--agentihooks", "--antoncore"])
def test_both_checkouts_are_required(tmp_path: Path, missing: str) -> None:
    roots = {"--agentihooks": _agentihooks(tmp_path, "BATCH_RUNNERS = 40\n"), "--antoncore": _antoncore(tmp_path, 40)}
    argv = [part for flag, root in roots.items() if flag != missing for part in (flag, str(root))]
    with pytest.raises(SystemExit) as exit_info:
        ceiling.main(argv)
    assert exit_info.value.code == 2


@pytest.mark.parametrize(("ours", "theirs"), [(41, 40), (40, 39)])
def test_a_planted_mismatch_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str], ours: int, theirs: int) -> None:
    agentihooks = _agentihooks(tmp_path, f"BATCH_RUNNERS = {ours}\n")
    status, lines = _run(capsys, agentihooks, _antoncore(tmp_path, theirs))
    assert status == 1
    assert lines == [
        f"::error::{ceiling.PLANNER} BATCH_RUNNERS is {ours} but antoncore {ceiling.BATCH_VALUES} maxRunners is"
        f" {theirs}; change them together."
    ]


@pytest.mark.parametrize("body", ["WORKERS = 4\n", "BATCH_RUNNERS = WORKERS * 10\n", "BATCH_RUNNERS: int\n"])
def test_a_planner_without_a_literal_ceiling_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], body: str
) -> None:
    status, lines = _run(capsys, _agentihooks(tmp_path, body), _antoncore(tmp_path, 40))
    assert status == 1
    assert lines == [f"::error::{ceiling.PLANNER} defines no integer literal BATCH_RUNNERS."]


def test_lint_grades_the_planner_ceiling_against_the_antoncore_dev_batch_chart() -> None:
    steps = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]["lint"]["steps"]
    checkout = next(s for s in steps if s.get("env", {}).get("REPOSITORY") == "the-cloud-clockwork/antoncore")
    assert checkout["env"]["REF"] == "dev"
    assert ceiling.BATCH_VALUES in checkout["env"]["SELECTED_PATHS"].split()
    check = next(s for s in steps if "scripts.ci_batch_runner_ceiling" in s.get("run", ""))
    assert check["if"] == "${{ !cancelled() }}"
    assert steps.index(checkout) < steps.index(check)
    assert '--agentihooks "$GITHUB_WORKSPACE" --antoncore "$GITHUB_WORKSPACE/.antoncore"' in check["run"]


@pytest.mark.parametrize(("graded", "expected"), [(True, "grader"), (False, "head")])
def test_lint_prefers_the_protected_grader_copy(tmp_path: Path, graded: bool, expected: str) -> None:
    steps = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]["lint"]["steps"]
    check = next(s for s in steps if "scripts.ci_batch_runner_ceiling" in s.get("run", ""))
    grader = tmp_path / "runner" / "grader"
    for root, name in ((tmp_path, "head"), (grader, "grader")):
        if root == grader and not graded:
            grader.mkdir(parents=True)
            continue
        (root / "scripts").mkdir(parents=True)
        (root / "scripts" / "__init__.py").write_text("")
        (root / "scripts" / "ci_batch_runner_ceiling.py").write_text(
            f"from pathlib import Path\nPath({str(tmp_path / 'graded')!r}).write_text({name!r})\n"
        )
    env = dict(os.environ, RUNNER_TEMP=str(tmp_path / "runner"), GITHUB_WORKSPACE=str(tmp_path), PYTHONSAFEPATH="")
    result = subprocess.run(["bash", "-e", "-c", check["run"]], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "graded").read_text() == expected
