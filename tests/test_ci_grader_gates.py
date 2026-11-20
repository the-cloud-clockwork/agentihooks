"""Lint and Semgrep gate steps grade the head with the protected grader's modules and config, the head only as data."""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import ci_marker_vectors

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
VECTORS = "tests/fixtures/marker-idempotency-vectors.json"
LINT_SELECT = 'select = ["E", "F", "I", "W"]'
QUOTE_STYLE = 'quote-style = "double"'
GRADER_CHECKOUT = "Check out the protected grader"
CHART_STEPS = {
    "chart_pod_templates.py": "A chart version bump leaves every pod template unchanged",
    "chart_timers.py": "Charts render no CronJob outside the scheduler",
}
UNWIRED = "on: pull_request\njobs:\n  planted:\n    runs-on: ubuntu-latest\n    steps:\n      - run: 'true'\n"
STALE = 'import argparse\n\nparser = argparse.ArgumentParser()\nparser.add_argument("vectors", nargs="?")\nparser.parse_args()\n'
PLANTED = 'print("planted head copy ran")\nraise SystemExit(0)\n'
BASE_GRADED = "0 rendered CronJob objects and 0 failed renders across 11 renders of 1 charts."
IGNORES_ROOT = f'print("{BASE_GRADED}")\nraise SystemExit(0)\n'
REFUSED = "The protected grader's copy of tests/ci_charts/{} does not grade the tree --root names"


def _steps(workflow: str, job: str) -> dict[str, dict[str, Any]]:
    jobs = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())["jobs"]
    return {step.get("name", ""): step for step in jobs[job]["steps"]}


def _lint(name: str) -> str:
    return _steps("test.yml", "lint")[name]["run"]


def _run(command: str, head: Path, runner: Path, grader: Path = ROOT) -> subprocess.CompletedProcess[str]:
    tools = runner / "bin"
    if not tools.exists():
        tools.mkdir(parents=True)
        (runner / "grader").symlink_to(grader)
        (tools / "python").write_text(f'#!/usr/bin/env bash\nexec "{sys.executable}" "$@"\n')
        (tools / "python").chmod(0o755)
    env = {
        **os.environ,
        "RUNNER_TEMP": str(runner),
        "GITHUB_WORKSPACE": str(head),
        "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
    }
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", command],
        cwd=head,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _python_head(tmp_path: Path) -> Path:
    head = tmp_path / "head"
    for folder in ("hooks", "scripts", "tests"):
        (head / folder).mkdir(parents=True)
    return head


def _planted_module(head: Path, module: str) -> None:
    (head / "scripts/__init__.py").write_text("")
    (head / f"scripts/{module}.py").write_text("raise SystemExit(0)\n")


def _stale_grader(tmp_path: Path, script: str, source: str = STALE) -> Path:
    grader = tmp_path / "stale-grader"
    (grader / script).parent.mkdir(parents=True)
    (grader / script).parent.joinpath("__init__.py").write_text("")
    (grader / script).write_text(source)
    return grader


@pytest.mark.feature("lint-gates-grade-from-grader")
def test_a_head_config_that_drops_a_rule_stays_red_under_the_grader_config(tmp_path: Path) -> None:
    head = _python_head(tmp_path)
    config = (ROOT / "pyproject.toml").read_text()
    assert LINT_SELECT in config
    (head / "pyproject.toml").write_text(config.replace(LINT_SELECT, 'select = ["E", "I", "W"]'))
    (head / "hooks/planted.py").write_text("import os\n")
    own = _run("python -P -m ruff check hooks/ scripts/ tests/", head, tmp_path / "runner")
    graded = _run(_lint("Lint check"), head, tmp_path / "runner")
    assert own.returncode == 0, own.stdout + own.stderr
    assert graded.returncode == 1, graded.stdout + graded.stderr
    assert "F401" in graded.stdout


@pytest.mark.feature("lint-gates-grade-from-grader")
def test_a_head_shadow_ruff_package_cannot_pass_the_lint_check(tmp_path: Path) -> None:
    head = _python_head(tmp_path)
    (head / "ruff").mkdir()
    (head / "ruff/__init__.py").write_text("")
    (head / "ruff/__main__.py").write_text("raise SystemExit(0)\n")
    (head / "hooks/planted.py").write_text("import os\n")
    graded = _run(_lint("Lint check"), head, tmp_path / "runner")
    assert graded.returncode == 1, graded.stdout + graded.stderr
    assert "F401" in graded.stdout


@pytest.mark.feature("lint-gates-grade-from-grader")
def test_the_lint_check_also_holds_a_rule_the_head_adds(tmp_path: Path) -> None:
    head = _python_head(tmp_path)
    config = (ROOT / "pyproject.toml").read_text()
    (head / "pyproject.toml").write_text(config.replace(LINT_SELECT, 'select = ["E", "F", "I", "W", "UP"]'))
    (head / "hooks/planted.py").write_text("from typing import List\n\nNAMES: List[str] = []\n")
    graded = _run(_lint("Lint check"), head, tmp_path / "runner")
    assert graded.returncode == 1, graded.stdout + graded.stderr
    assert "UP006" in graded.stdout


@pytest.mark.feature("lint-gates-grade-from-grader")
def test_a_head_formatter_config_stays_red_under_the_grader_config(tmp_path: Path) -> None:
    head = _python_head(tmp_path)
    config = (ROOT / "pyproject.toml").read_text()
    assert QUOTE_STYLE in config
    (head / "pyproject.toml").write_text(config.replace(QUOTE_STYLE, 'quote-style = "single"'))
    (head / "hooks/planted.py").write_text("NAME = 'planted'\n")
    own = _run("python -P -m ruff format --check hooks/ scripts/ tests/", head, tmp_path / "runner")
    graded = _run(_lint("Format check"), head, tmp_path / "runner")
    assert own.returncode == 0, own.stdout + own.stderr
    assert graded.returncode == 1, graded.stdout + graded.stderr
    assert "hooks/planted.py" in graded.stdout


@pytest.mark.feature("lint-gates-grade-from-grader")
def test_a_head_wiring_check_that_passes_itself_stays_red_under_the_grader_copy(tmp_path: Path) -> None:
    head = _python_head(tmp_path)
    shutil.copytree(ROOT / ".github/workflows", head / ".github/workflows")
    shutil.copy(ROOT / ".github/gate-wiring.json", head / ".github/gate-wiring.json")
    (head / ".github/workflows/planted.yml").write_text(UNWIRED)
    _planted_module(head, "ci_wiring")
    own = _run("python -m scripts.ci_wiring", head, tmp_path / "runner")
    graded = _run(_lint("Check every pull request job is a need of Gate Required"), head, tmp_path / "runner")
    assert own.returncode == 0, own.stdout + own.stderr
    assert graded.returncode == 1, graded.stdout + graded.stderr
    assert "planted.yml/planted runs on pull requests outside test.yml" in graded.stdout


@pytest.mark.feature("lint-gates-grade-from-grader")
def test_a_head_marker_check_that_passes_itself_stays_red_under_the_grader_copy(tmp_path: Path) -> None:
    head = _python_head(tmp_path)
    (head / "tests/fixtures").mkdir()
    (head / ".marker-kernel/tests/fixtures").mkdir(parents=True)
    (head / VECTORS).write_text('{"head": 1}\n')
    (head / ".marker-kernel" / VECTORS).write_text('{"kernel": 1}\n')
    _planted_module(head, "ci_marker_vectors")
    step = _lint("Refuse marker key vector drift from kernel dev")
    own = _run(f"python -m scripts.ci_marker_vectors .marker-kernel/{VECTORS}", head, tmp_path / "runner")
    graded = _run(step, head, tmp_path / "runner")
    assert own.returncode == 0, own.stdout + own.stderr
    assert graded.returncode == 1, graded.stdout + graded.stderr
    assert "Marker key vectors differ from kernel dev." in graded.stdout
    (head / ".marker-kernel" / VECTORS).write_text('{"head": 1}\n')
    matched = _run(step, head, tmp_path / "runner")
    assert matched.returncode == 0, matched.stdout + matched.stderr


@pytest.mark.feature("lint-gates-grade-from-grader")
def test_the_marker_check_reads_the_vectors_under_the_root_it_names(tmp_path: Path) -> None:
    (tmp_path / "tests/fixtures").mkdir(parents=True)
    (tmp_path / VECTORS).write_text('{"head": 1}\n')
    kernel = tmp_path / "kernel.json"
    kernel.write_text('{"head": 1}\n')
    assert ci_marker_vectors.main(["--root", str(tmp_path), str(kernel)]) == 0
    kernel.write_text('{"kernel": 1}\n')
    assert ci_marker_vectors.main(["--root", str(tmp_path), str(kernel)]) == 1


@pytest.mark.feature("lint-gates-grade-from-grader")
def test_the_marker_check_runs_after_the_grader_checkout() -> None:
    names = list(_steps("test.yml", "lint"))
    assert names.index(GRADER_CHECKOUT) < names.index("Refuse marker key vector drift from kernel dev")


@pytest.mark.feature("lint-gates-grade-from-grader")
@pytest.mark.parametrize("script", sorted(CHART_STEPS))
def test_a_chart_check_grades_the_tree_its_root_names(tmp_path: Path, script: str) -> None:
    (tmp_path / "deploy").mkdir()
    result = subprocess.run(
        [sys.executable, str(ROOT / "tests/ci_charts" / script), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "No chart found under deploy; the check graded nothing." in result.stdout


@pytest.mark.feature("lint-gates-grade-from-grader")
def test_a_head_chart_check_that_passes_itself_stays_red_under_the_grader_copy(tmp_path: Path) -> None:
    head = tmp_path / "head"
    (head / "deploy").mkdir(parents=True)
    (head / "tests/ci_charts").mkdir(parents=True)
    steps = _steps("semgrep.yml", "charts")
    for script, name in CHART_STEPS.items():
        (head / "tests/ci_charts" / script).write_text("raise SystemExit(0)\n")
        own = _run(f"python tests/ci_charts/{script}", head, tmp_path / "runner")
        graded = _run(steps[name]["run"], head, tmp_path / "runner")
        assert own.returncode == 0, own.stdout + own.stderr
        assert graded.returncode == 1, graded.stdout + graded.stderr
        assert "No chart found under deploy; the check graded nothing." in graded.stdout


@pytest.mark.feature("grader-gates-fail-closed-on-stale-base")
def test_a_stale_base_fails_the_marker_check_closed_and_a_planted_head_copy_never_runs(tmp_path: Path) -> None:
    head = _python_head(tmp_path)
    (head / "scripts/__init__.py").write_text("")
    (head / "scripts/ci_marker_vectors.py").write_text(PLANTED)
    grader = _stale_grader(tmp_path, "scripts/ci_marker_vectors.py")
    graded = _run(_lint("Refuse marker key vector drift from kernel dev"), head, tmp_path / "runner", grader)
    assert graded.returncode == 2, graded.stdout + graded.stderr
    assert "unrecognized arguments: --root" in graded.stderr
    assert "planted head copy ran" not in graded.stdout


@pytest.mark.feature("grader-gates-fail-closed-on-stale-base")
@pytest.mark.parametrize("script", sorted(CHART_STEPS))
def test_a_stale_base_fails_a_chart_check_closed_and_a_planted_head_copy_never_runs(
    tmp_path: Path, script: str
) -> None:
    head = tmp_path / "head"
    (head / "tests/ci_charts").mkdir(parents=True)
    (head / "tests/ci_charts" / script).write_text(PLANTED)
    grader = _stale_grader(tmp_path, f"tests/ci_charts/{script}")
    graded = _run(_steps("semgrep.yml", "charts")[CHART_STEPS[script]]["run"], head, tmp_path / "runner", grader)
    assert graded.returncode == 1, graded.stdout + graded.stderr
    assert REFUSED.format(script) in graded.stdout
    assert "planted head copy ran" not in graded.stdout


@pytest.mark.feature("chart-checks-refuse-a-grader-that-ignores-root")
@pytest.mark.parametrize("script", sorted(CHART_STEPS))
def test_a_chart_check_refuses_a_grader_copy_that_ignores_the_root_instead_of_grading_the_base(
    tmp_path: Path, script: str
) -> None:
    head = tmp_path / "head"
    (head / "deploy").mkdir(parents=True)
    grader = _stale_grader(tmp_path, f"tests/ci_charts/{script}", IGNORES_ROOT)
    graded = _run(_steps("semgrep.yml", "charts")[CHART_STEPS[script]]["run"], head, tmp_path / "runner", grader)
    assert graded.returncode == 1, graded.stdout + graded.stderr
    assert REFUSED.format(script) in graded.stdout
    assert BASE_GRADED not in graded.stdout


@pytest.mark.feature("lint-gates-grade-from-grader")
@pytest.mark.parametrize(
    "job,graded",
    [
        (
            "own-rules",
            ["Own rules hold on their fixtures and the code"],
        ),
        (
            "test-rules",
            ["Outage rules for tests hold on every test file", "New test waits keep a bound of ten seconds or more"],
        ),
        ("scan", ["Fail on findings new against the base"]),
        ("charts", sorted(CHART_STEPS.values())),
    ],
)
def test_each_semgrep_job_checks_out_the_caller_base_as_the_grader(job: str, graded: list[str]) -> None:
    steps = _steps("semgrep.yml", job)
    checkout = steps[GRADER_CHECKOUT]
    assert checkout["env"] == {"BASE": "${{ inputs.base }}"}
    assert checkout["run"].splitlines()[-1] == 'git worktree add --detach "$RUNNER_TEMP/grader" FETCH_HEAD'
    assert '"${BASE#origin/}"' in checkout["run"]
    reads = sorted(
        name for name, step in steps.items() if name != GRADER_CHECKOUT and "$RUNNER_TEMP/grader" in step.get("run", "")
    )
    assert reads == sorted(graded)
    names = list(steps)
    assert all(names.index(GRADER_CHECKOUT) < names.index(name) for name in graded)


@pytest.mark.feature("lint-gates-grade-from-grader")
def test_the_semgrep_scan_grades_the_code_with_the_grader_rules_and_the_head_rules() -> None:
    steps = _steps("semgrep.yml", "scan")
    own = _steps("semgrep.yml", "own-rules")["Own rules hold on their fixtures and the code"]["run"].splitlines()[-1]
    assert (
        own
        == 'semgrep scan --error --strict --timeout 30 --metrics off --config "$RUNNER_TEMP/grader/$RULES" --config "$RULES" .'
    )
    findings = steps["Fail on findings new against the base"]
    assert ".semgrep-incidents" in findings["env"]["CONFIGS"].split()
    assert 'args+=(--config "$RUNNER_TEMP/grader/.semgrep-incidents")' in findings["run"].splitlines()
