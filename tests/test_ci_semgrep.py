import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]


def _semgrep():
    return yaml.safe_load((_ROOT / ".github/workflows/semgrep.yml").read_text())


def _jobs():
    return _semgrep()["jobs"]


def _workflow(name):
    return yaml.safe_load((_ROOT / ".github/workflows" / name).read_text())


def test_wrapper_runs_the_inline_scan_feature_tests_and_chart_guard():
    assert set(_jobs()) == {"scan", "feature-tests", "charts"}


def test_scan_runs_inline_without_private_organization_workflow_calls():
    assert "uses" not in _jobs()["scan"]
    for workflow in (_ROOT / ".github/workflows").glob("*.yml"):
        document = yaml.safe_load(workflow.read_text())
        assert all(
            not job.get("uses", "").lower().startswith("the-cloud-clockwork/.github/")
            for job in document["jobs"].values()
        )


def test_scan_keeps_the_reusable_workflow_steps() -> None:
    steps = _jobs()["scan"]["steps"]
    assert steps[0] == {"uses": "actions/checkout@v4", "with": {"ref": "${{ github.sha }}", "fetch-depth": 0}}
    assert steps[1] == {"name": "Set up Python", "uses": "actions/setup-python@v5", "with": {"python-version": "3.12"}}
    assert [step.get("name") for step in steps[2:]] == [
        "Install Semgrep",
        "Install PyYAML",
        "Check out the protected grader",
        "Own rules hold on their fixtures and the code",
        "Outage rules for tests hold on every test file",
        "New test waits keep a bound of ten seconds or more",
        "Rule exceptions only shrink",
        "Resolve the merge base of the head and the base",
        "Fail on findings new against the base",
    ]
    assert steps[2]["env"] == {"SEMGREP_VERSION": "1.177.0"}
    assert steps[3] == {"name": "Install PyYAML", "run": "python -m pip install pyyaml==6.0.2"}
    assert _jobs()["scan"]["timeout-minutes"] == 5


def test_scan_grades_new_findings_against_the_caller_base():
    document = _semgrep()
    base = document[True]["workflow_call"]["inputs"]["base"]
    assert base["type"] == "string"
    assert base["required"] is True
    baseline = _jobs()["scan"]["steps"][-2]
    assert baseline["id"] == "baseline"
    assert baseline["env"] == {"BASE": "${{ inputs.base }}"}
    assert 'git merge-base "$BASE" HEAD' in baseline["run"]
    scan = _jobs()["scan"]["steps"][-1]
    assert scan["env"]["BASE"] == "${{ steps.baseline.outputs.sha }}"
    assert '--baseline-commit "$BASE"' in scan["run"]


def test_scan_runs_the_registry_packs_and_the_outage_rules():
    assert set(_jobs()["scan"]["steps"][-1]["env"]["CONFIGS"].split()) == {
        "p/ci",
        "p/secrets",
        "p/python",
        ".semgrep-incidents",
    }


def test_outage_rules_reach_the_base_graded_scan_and_the_fixture_step() -> None:
    assert ".semgrep-incidents" in _jobs()["scan"]["steps"][-1]["env"]["CONFIGS"].split()
    assert any(_ROOT.glob(".semgrep-incidents/*.yml"))
    runs = [step.get("run") for step in _jobs()["feature-tests"]["steps"]]
    fixture_run = next(run for run in runs if run and "cp -r .semgrep-incidents" in run)
    assert 'copy="$(mktemp -d -p "$RUNNER_TEMP")"' in fixture_run
    assert 'cp -r .semgrep-incidents "$copy/rules"' in fixture_run
    assert ': > "$copy/.semgrepignore"' in fixture_run
    assert '(cd "$copy" && semgrep --metrics off --test --config rules rules)' in fixture_run
    assert ".semgrep-incidents/" in (_ROOT / ".semgrepignore").read_text().splitlines()


TEST_RULES = "Outage rules for tests hold on every test file"
SCAN_STUB = '#!/usr/bin/env bash\necho "$*" >> "$STUBS/calls"\nexit "$SCAN_EXIT"\n'


def _test_rules_step() -> dict[str, Any]:
    return next(step for step in _jobs()["scan"]["steps"] if step.get("name") == TEST_RULES)


def _run_test_rules(tmp_path: Path, graded: bool, scan_exit: int = 0) -> tuple[int, list[str]]:
    step = _test_rules_step()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "semgrep").write_text(SCAN_STUB)
    (bin_dir / "semgrep").chmod(0o755)
    if graded:
        for rule in step["env"]["RULES"].split():
            (tmp_path / "grader" / rule).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / "grader" / rule).write_text("rules: []\n")
    env = {
        **os.environ,
        **step["env"],
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "RUNNER_TEMP": str(tmp_path),
        "STUBS": str(tmp_path),
        "SCAN_EXIT": str(scan_exit),
    }
    done = subprocess.run(["bash", "-euo", "pipefail", "-c", step["run"]], env=env, capture_output=True, text=True)
    calls = tmp_path / "calls"
    return done.returncode, calls.read_text().splitlines() if calls.exists() else []


def test_outage_rules_for_tests_scan_every_test_file_with_the_head_and_the_grader_copy(tmp_path: Path) -> None:
    rules = _test_rules_step()["env"]["RULES"].split()
    assert rules == [
        ".semgrep-incidents/ledger-client-swapped-for-a-function.yml",
        ".semgrep-incidents/parametrize-id-carries-interpreter-path.yml",
        ".semgrep-incidents/fake-server-close-read-ignores-reset.yml",
    ]
    configs = " ".join(f"--config {rule} --config {tmp_path}/grader/{rule}" for rule in rules)
    assert _run_test_rules(tmp_path, graded=True) == (
        0,
        [f"scan --error --strict --timeout 30 --metrics off --x-ignore-semgrepignore-files {configs} tests"],
    )


def test_outage_rules_for_tests_grade_with_the_head_copy_on_a_base_without_the_rule(tmp_path: Path) -> None:
    configs = " ".join(f"--config {rule}" for rule in _test_rules_step()["env"]["RULES"].split())
    assert _run_test_rules(tmp_path, graded=False) == (
        0,
        [f"scan --error --strict --timeout 30 --metrics off --x-ignore-semgrepignore-files {configs} tests"],
    )


def test_outage_rules_for_tests_fail_the_step_on_a_finding(tmp_path: Path) -> None:
    assert _run_test_rules(tmp_path, graded=True, scan_exit=1)[0] == 1


def test_every_semgrep_scan_passes_an_explicit_timeout() -> None:
    scans = [
        (shlex.split(line, comments=True), step.get("env", {}))
        for job in _jobs().values()
        for step in job["steps"]
        for line in step.get("run", "").splitlines()
        if line.split()[:2] == ["semgrep", "scan"]
    ]
    assert "tests" in [words[-1] for words, _ in scans]
    for words, env in scans:
        assert "--timeout" in words[:-1], words
        value = words[words.index("--timeout") + 1]
        assert str(env.get(value.removeprefix("$"), value)).isdigit(), words


def test_each_outage_rule_for_tests_reaches_the_tests_and_its_fixture() -> None:
    for name in _test_rules_step()["env"]["RULES"].split():
        path = _ROOT / name
        (rule,) = yaml.safe_load(path.read_text())["rules"]
        fixture = path.with_suffix(".py")
        assert rule["id"] == path.stem
        assert rule["paths"]["include"] == ["/tests/", fixture.name]
        text = fixture.read_text()
        assert f"# ruleid: {rule['id']}" in text
        assert f"# ok: {rule['id']}" in text
    assert "tests/" in (_ROOT / ".semgrepignore").read_text().splitlines()


WAIT_BOUNDS = "New test waits keep a bound of ten seconds or more"
WAIT_SCAN = "scan --error --strict --timeout 30 --metrics off --x-ignore-semgrepignore-files"


def _wait_bounds_step() -> dict[str, Any]:
    return next(step for step in _jobs()["scan"]["steps"] if step.get("name") == WAIT_BOUNDS)


def _run_wait_bounds(
    tmp_path: Path, graded: bool, scan_exit: int = 0, base: str | None = None
) -> tuple[int, list[str], str]:
    step = _wait_bounds_step()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "dev")
    merge_base = _commit(repo, {"tests/test_kept.py": "def test_kept() -> None:\n    assert True\n"}, "base")
    _git(repo, "checkout", "-q", "-b", "pr")
    _commit(repo, {"tests/test_new.py": "def test_new() -> None:\n    assert True\n"}, "head")
    _git(repo, "checkout", "-q", "dev")
    moved = _commit(repo, {"tests/test_later.py": "def test_later() -> None:\n    assert True\n"}, "dev")
    _git(repo, "checkout", "-q", "pr")
    tools = tmp_path / "bin"
    tools.mkdir()
    (tools / "semgrep").write_text(SCAN_STUB)
    (tools / "semgrep").chmod(0o755)
    runner = tmp_path / "runner"
    rule = step["env"]["RULE"]
    if graded:
        (runner / "grader" / rule).parent.mkdir(parents=True)
        (runner / "grader" / rule).write_text("rules: []\n")
    env = {
        **os.environ,
        **step["env"],
        "BASE": base or moved,
        "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
        "RUNNER_TEMP": str(runner),
        "STUBS": str(tmp_path),
        "SCAN_EXIT": str(scan_exit),
    }
    done = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    calls = tmp_path / "calls"
    return done.returncode, calls.read_text().splitlines() if calls.exists() else [], merge_base


@pytest.mark.feature("test-wait-bound-ratchet")
def test_new_test_waits_scan_the_tests_against_the_merge_base_with_the_head_and_the_grader_rule(
    tmp_path: Path,
) -> None:
    rule = _wait_bounds_step()["env"]["RULE"]
    code, calls, merge_base = _run_wait_bounds(tmp_path, graded=True)
    configs = f"--config {rule} --config {tmp_path}/runner/grader/{rule}"
    assert (code, calls) == (0, [f"{WAIT_SCAN} {configs} --baseline-commit {merge_base} tests"])


@pytest.mark.feature("test-wait-bound-ratchet")
def test_new_test_waits_grade_with_the_head_rule_on_a_base_without_it(tmp_path: Path) -> None:
    rule = _wait_bounds_step()["env"]["RULE"]
    code, calls, merge_base = _run_wait_bounds(tmp_path, graded=False)
    assert (code, calls) == (0, [f"{WAIT_SCAN} --config {rule} --baseline-commit {merge_base} tests"])


@pytest.mark.feature("test-wait-bound-ratchet")
def test_a_scan_finding_fails_the_new_test_waits_step(tmp_path: Path) -> None:
    assert _run_wait_bounds(tmp_path, graded=True, scan_exit=1)[0] == 1


@pytest.mark.feature("test-wait-bound-ratchet")
def test_new_test_waits_fail_without_scanning_when_the_base_is_unknown(tmp_path: Path) -> None:
    code, calls, _ = _run_wait_bounds(tmp_path, graded=True, base="0" * 40)
    assert code != 0
    assert calls == []


@pytest.mark.feature("test-wait-bound-ratchet")
def test_the_wait_bound_rule_reaches_the_tests_and_its_fixture_under_ten_seconds() -> None:
    path = _ROOT / _wait_bounds_step()["env"]["RULE"]
    (rule,) = yaml.safe_load(path.read_text())["rules"]
    fixture = path.with_suffix(".py")
    assert rule["id"] == path.stem
    assert rule["paths"]["include"] == ["/tests/", fixture.name]
    comparisons = [entry["metavariable-comparison"] for entry in rule["patterns"] if "metavariable-comparison" in entry]
    assert comparisons == [{"metavariable": "$BOUND", "comparison": "$BOUND > 0 and $BOUND < 10"}]
    text = fixture.read_text()
    assert f"# ruleid: {rule['id']}" in text
    assert f"# ok: {rule['id']}" in text


@pytest.mark.feature("semgrep-feature-tests")
def test_feature_fixture_command_runs_recursively():
    job = _jobs()["feature-tests"]
    commands = [step["run"] for step in job["steps"] if "run" in step]
    assert "semgrep --metrics off --test .semgrep/features" in commands
    assert "if" not in job and "continue-on-error" not in job
    assert all("if" not in step and "continue-on-error" not in step for step in job["steps"])


def test_no_semgrep_job_runs_the_head_feature_map_validator() -> None:
    steps = [step for job in _jobs().values() for step in job.get("steps", [])]
    assert not any("scripts.feature_map" in step.get("run", "") for step in steps)


def test_source_control_rule_covers_swarm_calls_and_uses_explicit_exceptions() -> None:
    import json

    rule = yaml.safe_load((_ROOT / ".semgrep/source-control.yml").read_text())["rules"][0]
    assert rule["id"] == "swarm-source-control-port"
    assert rule["languages"] == ["python"]
    assert rule["severity"] == "ERROR"
    assert rule["pattern-either"] == [
        {"pattern": '["gh", ...]'},
        {"pattern": "scripts.tick.adapters.github.command(...)"},
    ]
    assert rule["paths"]["include"] == ["/scripts/swarm/**", "/scripts/tick/**", "*.test.py"]
    allowed = json.loads((_ROOT / ".semgrep/source-control-allowlist.json").read_text())
    assert rule["paths"]["exclude"] == allowed
    assert "/scripts/tick/adapters/github.py" in allowed
    assert all(path.endswith(".py") and "*" not in path for path in allowed)
    assert "/scripts/swarm/publish.py" not in allowed
    assert "/scripts/swarm/github_sampler.py" not in allowed
    assert "/scripts/swarm/stack.py" not in allowed
    assert "/scripts/swarm/panic.py" not in allowed
    assert "/scripts/swarm/linked_issues.py" not in allowed
    assert "/scripts/swarm/ready.py" not in allowed
    assert "/scripts/swarm/ledger_host.py" not in allowed
    assert "/scripts/swarm/health/checks.py" not in allowed
    assert "/scripts/swarm/fleet_red.py" not in allowed
    assert "/scripts/swarm/cli.py" not in allowed
    assert "/scripts/swarm/ci_speed.py" not in allowed
    assert "/scripts/swarm/run_wait.py" not in allowed
    assert "/scripts/swarm/ledger_events.py" not in allowed
    assert allowed == ["/scripts/swarm/github_reads.py", "/scripts/tick/adapters/github.py"]


@pytest.mark.feature("tick-unbounded-read-rule")
def test_source_control_allowlist_is_graded_against_the_caller_base() -> None:
    steps = _jobs()["scan"]["steps"]
    step = next(step for step in steps if step.get("name") == "Rule exceptions only shrink")
    assert step["env"]["BASE"] == "${{ inputs.base }}"
    assert "for list in .semgrep/*-allowlist.json; do" in step["run"]
    assert 'git show "$BASE:$list"' in step["run"]
    assert "current <= previous" in step["run"]
    lists = sorted(path.name for path in (_ROOT / ".semgrep").glob("*-allowlist.json"))
    assert lists == ["source-control-allowlist.json", "tick-unbounded-read-allowlist.json"]


@pytest.mark.feature("tick-unbounded-read-rule")
def test_unbounded_read_rule_covers_tick_code_and_lists_each_exception_with_a_task() -> None:
    rule = yaml.safe_load((_ROOT / ".semgrep/tick-unbounded-read.yml").read_text())["rules"][0]
    assert rule["id"] == "tick-unbounded-store-read"
    assert rule["languages"] == ["python"]
    assert rule["severity"] == "ERROR"
    assert rule["paths"]["include"] == ["/scripts/swarm/**", "/scripts/tick/**", "*.test.py"]
    allowed = json.loads((_ROOT / ".semgrep/tick-unbounded-read-allowlist.json").read_text())
    assert rule["paths"]["exclude"] == sorted(allowed)
    assert all(path.endswith(".py") and "*" not in path for path in allowed)
    assert all(isinstance(task, str) and task.startswith("t-") for task in allowed.values())
    fixture = (_ROOT / ".semgrep/tick-unbounded-read.test.py").read_text()
    assert fixture.count("# ruleid: tick-unbounded-store-read") == 12
    assert fixture.count("# ok: tick-unbounded-store-read") == 10


def test_ledger_write_rules_carry_no_exceptions() -> None:
    source = (_ROOT / ".semgrep/ledger-server-writes.yml").read_text()
    rules = {rule["id"]: rule for rule in yaml.safe_load(source)["rules"]}
    assert sorted(rules) == ["ledger-client-in-process", "ledger-server-reads-only"]
    assert all(rule["languages"] == ["python"] and rule["severity"] == "ERROR" for rule in rules.values())
    assert "paths" not in rules["ledger-client-in-process"]
    assert rules["ledger-server-reads-only"]["paths"] == {
        "include": ["/scripts/swarm_ledger/ledger_server.py", "*.test.py"]
    }
    assert source.count("pattern-not") == 3
    assert not (_ROOT / ".semgrep/ledger-server-writes-allowlist.json").exists()
    fixture = (_ROOT / ".semgrep/ledger-server-writes.test.py").read_text()
    assert fixture.count("# ruleid: ledger-server-reads-only") == 10
    assert fixture.count("# ok: ledger-server-reads-only") == 3
    assert fixture.count("# ruleid: ledger-client-in-process") == 8
    assert fixture.count("# ok: ledger-client-in-process") == 4
    incidents = yaml.safe_load((_ROOT / ".semgrep-incidents/ledger-server-write.yml").read_text())
    assert [rule["id"] for rule in incidents["rules"]] == ["ledger-write-switch"]


EXCEPTION_RULE = """rules:
  - id: planted-rule
    languages: [python]
    severity: ERROR
    message: planted
    patterns:
      - pattern: planted(...)
      - pattern-not: planted(1)
    paths:
      exclude: [/kept.py]
"""
GROWN_NOT = EXCEPTION_RULE.replace("planted(1)\n", "planted(1)\n      - pattern-not-inside: kept(...)\n")
SHRUNK = EXCEPTION_RULE.replace("      - pattern-not: planted(1)\n", "").replace("[/kept.py]", "[]")
SAME_COUNTS = (
    "Own rule exceptions: 1 pattern-not entries and 1 path excludes in 1 rules; the merge base holds 1 and 1 in 1 rules"
)
EXCEPTION_BASE: dict[str, str | None] = {
    ".semgrep/planted-allowlist.json": '["/kept.py"]\n',
    ".semgrep/features/planted.yml": EXCEPTION_RULE,
    ".semgrep/features/planted.test.yaml": "rules:\n  - id: planted-rule\n    pattern-not: one\n",
}
EXCEPTION_CASES: dict[str, tuple[dict[str, str | None], dict[str, str | None], int, str]] = {
    "unchanged": ({}, {}, 0, SAME_COUNTS),
    "pattern-not-grows": (
        {},
        {".semgrep/features/planted.yml": GROWN_NOT},
        1,
        "planted-rule pattern-not entries grew from 1 to 2",
    ),
    "path-exclude-grows": (
        {},
        {".semgrep/features/planted.yml": EXCEPTION_RULE.replace("[/kept.py]", "[/kept.py, /new.py]")},
        1,
        "planted-rule path excludes grew from 1 to 2",
    ),
    "exceptions-shrink": (
        {},
        {".semgrep/features/planted.yml": SHRUNK},
        0,
        "Own rule exceptions: 0 pattern-not entries and 0 path excludes in 1 rules; the merge base holds 1 and 1 in 1 rules",
    ),
    "dev-shrinks-after-the-branch": ({".semgrep/features/planted.yml": SHRUNK}, {}, 0, SAME_COUNTS),
    "rule-moves-between-folders": (
        {},
        {".semgrep/features/planted.yml": None, ".semgrep-incidents/planted.yml": EXCEPTION_RULE},
        0,
        SAME_COUNTS,
    ),
    "new-rule-starts-its-counts": (
        {},
        {".semgrep-incidents/new.yml": GROWN_NOT.replace("planted-rule", "new-rule")},
        0,
        "Own rule exceptions: 3 pattern-not entries and 2 path excludes in 2 rules; the merge base holds 1 and 1 in 1 rules",
    ),
    "fixture-is-not-a-rule": (
        {},
        {
            ".semgrep/features/planted.test.yaml": "rules:\n  - id: planted-rule\n    pattern-not: one\n    pattern-not-inside: two\n"
        },
        0,
        SAME_COUNTS,
    ),
    "no-rule-file": (
        {},
        {".semgrep/features/planted.yml": None},
        1,
        "No own Semgrep rule under a .semgrep folder; the check graded nothing",
    ),
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, files: dict[str, str | None], message: str) -> str:
    for name, text in files.items():
        path = repo / name
        if text is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _run_exceptions_step(
    tmp_path: Path, dev: dict[str, str | None], head: dict[str, str | None]
) -> subprocess.CompletedProcess[str]:
    step = next(step for step in _jobs()["scan"]["steps"] if step.get("name") == "Rule exceptions only shrink")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "dev")
    _commit(repo, EXCEPTION_BASE, "base")
    _git(repo, "checkout", "-q", "-b", "pr")
    _commit(repo, head, "head")
    _git(repo, "checkout", "-q", "dev")
    base = _commit(repo, dev, "dev")
    _git(repo, "checkout", "-q", "pr")
    tools = tmp_path / "bin"
    tools.mkdir()
    (tools / "python").write_text(f'#!/usr/bin/env bash\nexec "{sys.executable}" "$@"\n')
    (tools / "python").chmod(0o755)
    env = {**os.environ, "BASE": base, "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}"}
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.feature("semgrep-rule-exceptions-shrink")
@pytest.mark.parametrize("dev,head,code,line", list(EXCEPTION_CASES.values()), ids=list(EXCEPTION_CASES))
def test_the_exceptions_step_counts_each_own_rule_pattern_not_entry_and_path_exclude(
    tmp_path: Path, dev: dict[str, str | None], head: dict[str, str | None], code: int, line: str
) -> None:
    done = _run_exceptions_step(tmp_path, dev, head)
    assert done.returncode == code, done.stdout + done.stderr
    assert line in done.stdout + done.stderr
    assert ".semgrep/planted-allowlist.json exceptions: 1 of 1" in done.stdout


def test_scan_excludes_only_the_mutable_action_tag_rule():
    assert _jobs()["scan"]["steps"][-1]["env"]["EXCLUDE_RULES"].split() == [
        "yaml.github-actions.security.github-actions-mutable-action-tag.github-actions-mutable-action-tag"
    ]


def test_scan_reads_the_domain_rules_folder_that_holds_rule_files() -> None:
    step = next(
        step for step in _jobs()["scan"]["steps"] if step.get("name") == "Own rules hold on their fixtures and the code"
    )
    rules_dir = _ROOT / step["env"]["RULES"]
    assert rules_dir == _ROOT / ".semgrep"
    assert any(_ROOT.glob(".semgrep/*.yml"))
    assert "semgrep --metrics off --test --config rules rules" in step["run"]
    assert '--config "$RUNNER_TEMP/grader/$RULES" --config "$RULES" .' in step["run"]


def test_own_rules_scan_gives_each_rule_thirty_seconds_on_a_file() -> None:
    step = next(
        step for step in _jobs()["scan"]["steps"] if step.get("name") == "Own rules hold on their fixtures and the code"
    )
    assert "semgrep scan --error --strict --timeout 30 --metrics off" in step["run"]


def test_scan_and_charts_run_on_the_select_runner_expression():
    jobs = _jobs()
    select = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]["select"]
    assert jobs["scan"]["runs-on"] == jobs["charts"]["runs-on"] == select["runs-on"]


def test_charts_run_the_chart_timer_guard() -> None:
    steps = _jobs()["charts"]["steps"]
    assert steps[-1]["run"].splitlines()[0] == "script=tests/ci_charts/chart_timers.py"
    assert 'python "$RUNNER_TEMP/grader/$script" --root "$GITHUB_WORKSPACE"' in steps[-1]["run"]
    assert (_ROOT / "tests/ci_charts/chart_timers.py").is_file()


def test_charts_render_every_pod_template_at_two_chart_versions() -> None:
    step = next(
        step
        for step in _jobs()["charts"]["steps"]
        if step.get("name") == "A chart version bump leaves every pod template unchanged"
    )
    assert step["run"].splitlines()[0] == "script=tests/ci_charts/chart_pod_templates.py"
    assert 'python "$RUNNER_TEMP/grader/$script" --root "$GITHUB_WORKSPACE"' in step["run"]
    assert (_ROOT / "tests/ci_charts/chart_pod_templates.py").is_file()


def test_charts_grade_the_guard_rules_against_the_base_floors():
    steps = _jobs()["charts"]["steps"]
    install = next(step for step in steps if step.get("name") == "Install PyYAML and Semgrep")
    guard = next(
        step for step in steps if step.get("name") == "Guard rules hold on their fixtures and at their computed floors"
    )
    assert install["env"] == {"SEMGREP_VERSION": "1.177.0"}
    assert guard["env"] == {"BASE": "${{ inputs.base }}"}
    assert guard["run"].splitlines() == [
        'git fetch --no-tags --depth=1 origin "${BASE#origin/}"',
        "grader=scripts/ci_semgrep_floors.py",
        "if git cat-file -e FETCH_HEAD:scripts/ci_semgrep_floors.py; then",
        '  grader="$RUNNER_TEMP/ci_semgrep_floors.py"',
        '  git show FETCH_HEAD:scripts/ci_semgrep_floors.py > "$grader"',
        "fi",
        'python "$grader" check --base FETCH_HEAD',
    ]
    assert steps.index(install) < steps.index(guard) < len(steps) - 1


SEMGREP_STUB = '#!/usr/bin/env bash\n[[ -f "$STUBS/version" ]] || exit 127\ncat "$STUBS/version"\n'
PYTHON_STUB = (
    '#!/usr/bin/env bash\necho "$*" >> "$STUBS/calls"\n'
    'if [[ "$*" == *semgrep==* ]]; then echo "$PIP_GIVES" > "$STUBS/version"; fi\n'
)


def _run_install(
    tmp_path: Path,
    installed: str,
    pip_gives: str = "1.177.0",
    job: str = "charts",
    name: str = "Install PyYAML and Semgrep",
) -> tuple[int, list[str]]:
    install = next(step for step in _jobs()[job]["steps"] if step.get("name") == name)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python").write_text(PYTHON_STUB)
    (bin_dir / "semgrep").write_text(SEMGREP_STUB)
    for tool in bin_dir.iterdir():
        tool.chmod(0o755)
    if installed:
        (tmp_path / "version").write_text(installed + "\n")
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "STUBS": str(tmp_path), "PIP_GIVES": pip_gives}
    env.update(install["env"])
    done = subprocess.run(["bash", "-euo", "pipefail", "-c", install["run"]], env=env, capture_output=True, text=True)
    calls = tmp_path / "calls"
    return done.returncode, calls.read_text().splitlines() if calls.exists() else []


def test_charts_use_the_runner_semgrep_when_it_matches_the_pin(tmp_path):
    assert _run_install(tmp_path, "1.177.0") == (0, ["-m pip install pyyaml==6.0.2"])


@pytest.mark.parametrize("installed", ["", "1.176.0"])
def test_charts_install_the_pinned_semgrep_without_a_matching_runner_copy(tmp_path, installed):
    assert _run_install(tmp_path, installed) == (0, ["-m pip install pyyaml==6.0.2 semgrep==1.177.0"])


def test_charts_fail_when_the_install_leaves_another_semgrep(tmp_path):
    assert _run_install(tmp_path, "", pip_gives="1.176.0") == (1, ["-m pip install pyyaml==6.0.2 semgrep==1.177.0"])


def test_feature_tests_use_the_runner_semgrep_when_it_matches_the_pin(tmp_path: Path) -> None:
    assert _run_install(tmp_path, "1.177.0", job="feature-tests", name="Install Semgrep") == (0, [])


@pytest.mark.parametrize("installed", ["", "1.176.0"])
def test_feature_tests_install_the_pinned_semgrep_without_a_matching_runner_copy(
    tmp_path: Path, installed: str
) -> None:
    install = _run_install(tmp_path, installed, job="feature-tests", name="Install Semgrep")
    assert install == (0, ["-m pip install semgrep==1.177.0"])


def test_feature_tests_fail_when_the_install_leaves_another_semgrep(tmp_path: Path) -> None:
    install = _run_install(tmp_path, "", pip_gives="1.176.0", job="feature-tests", name="Install Semgrep")
    assert install == (1, ["-m pip install semgrep==1.177.0"])


def test_feature_tests_bound_the_semgrep_install_below_the_job_timeout() -> None:
    job = _jobs()["feature-tests"]
    install = next(step for step in job["steps"] if step.get("name") == "Install Semgrep")
    assert 0 < install.get("timeout-minutes", job["timeout-minutes"]) < job["timeout-minutes"]


def test_feature_tests_install_semgrep_with_the_scan_check_and_pin() -> None:
    steps = {(job, step.get("name")): step for job, spec in _jobs().items() for step in spec["steps"]}
    feature, scan = steps["feature-tests", "Install Semgrep"], steps["scan", "Install Semgrep"]
    assert (feature["run"], feature["env"]) == (scan["run"], scan["env"])


def test_no_semgrep_job_or_step_can_pass_on_a_skip_or_an_error():
    jobs = _jobs()
    assert not {"if", "continue-on-error"} & (set(jobs["scan"]) | set(jobs["charts"]))
    assert all("if" not in step and "continue-on-error" not in step for job in jobs.values() for step in job["steps"])


def test_wrapper_holds_only_read_permission():
    assert _semgrep()["permissions"] == {"contents": "read"}


def test_feature_tests_and_charts_have_runner_classes():
    classes = json.loads((_ROOT / ".github/runner-classes.json").read_text())
    assert {key for key in classes if key.startswith("semgrep.yml/")} == {
        "semgrep.yml/charts",
        "semgrep.yml/feature-tests",
        "semgrep.yml/scan",
    }


def test_exactly_one_step_per_ratchet_grades_against_the_base():
    ratchets = [
        (job, step)
        for job, body in _jobs().items()
        for step in body.get("steps", [])
        if "check --base FETCH_HEAD" in step.get("run", "") or "--baseline-commit" in step.get("run", "")
    ]
    assert [(job, step["name"]) for job, step in ratchets] == [
        ("scan", "New test waits keep a bound of ten seconds or more"),
        ("scan", "Fail on findings new against the base"),
        ("charts", "Guard rules hold on their fixtures and at their computed floors"),
    ]
    assert all(not {"if", "continue-on-error"} & set(step) for _, step in ratchets)


def test_wrapper_is_triggered_only_by_workflow_call():
    assert set(_semgrep()[True]) == {"workflow_call"}


def test_tests_workflow_scans_pull_requests_into_dev_and_admitted_dispatches_only():
    tests = _workflow("test.yml")
    assert tests[True]["pull_request"] == {"branches": ["dev"]}
    job = tests["jobs"]["semgrep"]
    assert job["uses"] == "./.github/workflows/semgrep.yml"
    for clause in (
        "(github.event_name != 'workflow_dispatch' || needs.admission.result == 'success')",
        "github.event_name != 'merge_group'",
        "github.event_name != 'push'",
    ):
        assert clause in job["if"]


def test_proofs_workflow_scans_only_on_the_semgrep_proof_against_its_base():
    job = _workflow("proofs.yml")["jobs"]["semgrep"]
    assert job["uses"] == "./.github/workflows/semgrep.yml"
    assert job["if"] == "inputs.proof == 'semgrep'"
    assert job["with"] == {"base": "${{ inputs.base }}"}


def test_scan_holds_the_status_the_dev_rulesets_require():
    gate = _workflow("test.yml")["jobs"]["gate-required"]
    assert gate["name"] == "${{ github.event_name == 'merge_group' && 'Queue gate summary' || 'Gate — Required' }}"
    assert "semgrep" in gate["needs"]
    grader = next(step for step in gate["steps"] if step.get("name") == "Require every gate to succeed")
    assert '["select", "test-count", "lint", "type-gate", "docs", "semgrep", "sonar", "writer-lock"]' in grader["run"]
    for ruleset in ("dev-history.json", "dev-no-delete.json"):
        rules = json.loads((_ROOT / ".github/rulesets" / ruleset).read_text())["rules"]
        checks = next(rule for rule in rules if rule["type"] == "required_status_checks")
        assert [check["context"] for check in checks["parameters"]["required_status_checks"]] == ["Gate — Required"]


def test_caller_and_wrapper_jobs_keep_their_job_id_check_names():
    assert "name" not in _workflow("test.yml")["jobs"]["semgrep"]
    assert all("name" not in job for job in _jobs().values())
