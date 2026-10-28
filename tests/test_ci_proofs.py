import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tests.mutation_sandbox_input_proof import REPORT_ARTIFACT, SPLIT_ALGORITHM, nested_report

_ROOT = Path(__file__).resolve().parents[1]


def _workflow():
    return yaml.safe_load((_ROOT / ".github/workflows/proofs.yml").read_text())


def test_proofs_dispatch_cannot_satisfy_required_tests():
    workflow = _workflow()
    assert set(workflow[True]) == {"workflow_dispatch"}
    assert workflow["concurrency"]["group"].startswith(
        "${{ inputs.proof == 'tests' && format('focused-proof-{0}', github.run_id) || format('hosted-lane-{0}', "
    )
    assert (workflow["concurrency"]["queue"], workflow["concurrency"]["cancel-in-progress"]) == ("max", False)
    assert workflow["permissions"] == {"contents": "read", "pull-requests": "read", "actions": "read"}
    inputs = workflow[True]["workflow_dispatch"]["inputs"]
    assert inputs["proof"]["type"] == "choice"
    assert inputs["proof"]["required"] is True
    assert set(inputs["proof"]["options"]) == set(workflow["jobs"])
    assert "gate plant proofs must use the full Tests dispatch" in inputs["proof"]["description"]
    assert all(job.get("name", key) != "Gate — Required" for key, job in workflow["jobs"].items())
    required = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())
    assert {"pull_request", "push", "merge_group", "workflow_dispatch"} <= set(required[True])
    from scripts import ci_gate_queue

    required = ci_gate_queue.normalize({"test.yml": required})["test.yml"]
    assert required["jobs"]["gate-required"]["name"] == "Gate — Required"
    assert "proof" not in required[True]["workflow_dispatch"]["inputs"]


@pytest.mark.parametrize(
    ("paths", "code"),
    [
        ("tests/test_a.py tests/test_b.py::test_c", 0),
        ("", 1),
        ("tests/test_a.py\ntests/test_b.py", 1),
        ("tests/../hooks", 1),
        ("hooks/test_a.py", 1),
    ],
)
def test_a_focused_proof_is_red_unless_every_test_path_is_on_one_line_under_tests(paths, code, tmp_path):
    step = next(s for s in _workflow()["jobs"]["tests"]["steps"] if s.get("name") == "Require explicit test paths")
    (tmp_path / "tests").mkdir()
    for name in ("test_a.py", "test_b.py"):
        (tmp_path / "tests" / name).touch()
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env={**os.environ, "TEST_PATHS": paths},
        cwd=tmp_path,
        capture_output=True,
    )
    assert result.returncode == code, result.stdout + result.stderr


def test_a_focused_proof_sizes_pytest_workers_from_the_cpu_quota():
    step = next(s for s in _workflow()["jobs"]["tests"]["steps"] if s.get("name") == "Run selected tests")
    lines = [line.strip() for line in step["run"].splitlines() if "workers" in line]
    assert lines == [
        "workers=$(python -m scripts.ci_cpu_quota)",
        'python -m pytest "${paths[@]}" -v -n "$workers" '
        '--dist loadgroup -p tests.flake_ledger --tb=short 2>&1 | tee "$log" || status=$?',
    ]


@pytest.mark.parametrize(
    ("log", "exit_code", "code"),
    [
        ("INTERNALERROR> Traceback (most recent call last):\n=== no tests ran in 0.50s ===\n", 0, 1),
        ("[gw0] PASSED tests/test_a.py::test_a\nINTERNALERROR> KeyError\n=== 1 passed in 0.50s ===\n", 0, 1),
        ("=== no tests ran in 0.50s ===\n", 0, 1),
        ("=== 3 skipped in 0.10s ===\n", 0, 1),
        ("collecting ...\n", 0, 1),
        ("=== 2 passed in 1.20s ===\n", 0, 0),
        ("[gw0] [ 50%] PASSED tests/test_a.py::test_a[INTERNALERROR> KeyError]\n=== 1 passed in 0.20s ===\n", 0, 0),
        ("=== 2 passed, 1 skipped, 3 warnings in 75.32s (0:01:15) ===\n", 0, 0),
        ("\x1b[32m=== \x1b[32m\x1b[1m2 passed\x1b[0m\x1b[32m in 1.20s\x1b[0m\x1b[32m ===\x1b[0m\n", 0, 0),
        ("=== 1 failed, 2 passed in 1.20s ===\n", 1, 1),
        ("=== 2 passed in 1.20s ===\n", 3, 3),
    ],
)
def test_a_focused_proof_is_red_unless_a_test_completed_without_an_internal_error(log, exit_code, code, tmp_path):
    step = next(s for s in _workflow()["jobs"]["tests"]["steps"] if s.get("name") == "Run selected tests")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "pytest.log").write_text(log)
    stub = bin_dir / "python"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'[[ "$1 $2" != "-m pytest" ]] || {{ cat "{tmp_path / "pytest.log"}"; exit {exit_code}; }}\n'
        '[[ "$1 $2" != "-m scripts.ci_cpu_quota" ]] || { echo 2; exit 0; }\n'
        f'exec "{sys.executable}" "$@"\n'
    )
    stub.chmod(0o755)
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "TEST_PATHS": "tests/test_a.py",
            "RUNNER_TEMP": str(tmp_path),
        },
        cwd=_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == code, result.stdout + result.stderr
    assert (tmp_path / "focused-proof.log").read_text() == log
    assert ("::error title=Focused proof::" in result.stdout) == (code == 1 and exit_code == 0)


def test_the_proof_outcome_names_why_a_run_proved_nothing(tmp_path, capsys):
    from scripts import ci_proof_outcome

    green = tmp_path / "green.log"
    green.write_text("=== 1 xfailed, 1 xpassed in 0.20s ===\n")
    crashed = tmp_path / "crashed.log"
    crashed.write_text("INTERNALERROR> worker gw0 crashed\n=== 1 passed in 0.20s ===\n")
    assert ci_proof_outcome.main([str(green)]) == 0
    assert capsys.readouterr().out == ""
    assert ci_proof_outcome.main([str(crashed)]) == 1
    assert capsys.readouterr().out == "::error title=Focused proof::pytest raised an internal error.\n"
    assert ci_proof_outcome.verdict("=== 4 deselected in 0.10s ===") == "No selected test completed."


@pytest.mark.parametrize(
    ("line", "head"),
    [
        ("=== 2 passed in 1.20s ===", "2 passed"),
        ("  == 3 passed, 1 failed in 75.32s (0:01:15) ==  ", "3 passed, 1 failed"),
        ("=== 2 passed in a in 1.00s ===", "2 passed in a"),
        ("=== 1 passed in tests/x ===", None),
        ("2 passed in 1.20s ===", None),
        ("=== 2 passed in 1.20s", None),
        ("=== FAILURES ===", None),
    ],
)
def test_the_proof_outcome_reads_only_pytest_summary_lines(line, head):
    from scripts import ci_proof_outcome

    assert ci_proof_outcome.summary(line) == head


@pytest.mark.parametrize(
    ("log", "reason"),
    [
        ("\x1b[32m=== \x1b[1m2 passed\x1b[0m\x1b[32m in 1.20s ===\x1b[0m\n", None),
        ("\x1b[31mINTERNALERROR> boom\x1b[0m\n=== 2 passed in 1.20s ===\n", "pytest raised an internal error."),
        ("[gw3] INTERNALERROR> boom\n=== 2 passed in 1.20s ===\n", "pytest raised an internal error."),
        ("=== 1 passed in 1.00s ===\n=== no tests ran in 1.00s ===\n", "No selected test completed."),
    ],
)
def test_the_proof_outcome_reads_colored_and_worker_output(log, reason):
    from scripts import ci_proof_outcome

    assert ci_proof_outcome.verdict(log) == reason


def test_the_proof_outcome_reads_a_log_with_undecodable_bytes(tmp_path):
    from scripts import ci_proof_outcome

    log = tmp_path / "proof.log"
    log.write_bytes(b"\xff\xfe output\n=== 1 passed in 0.10s ===\n")
    assert ci_proof_outcome.main([str(log)]) == 0


@pytest.mark.parametrize(
    "paths", ["tests/missing.py", "tests/missing.py::test_case", "tests/existing.py tests/missing.py"]
)
def test_a_focused_proof_refuses_missing_paths_before_setup(paths, tmp_path):
    steps = _workflow()["jobs"]["tests"]["steps"]
    index = next(i for i, step in enumerate(steps) if step.get("name") == "Require explicit test paths")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/existing.py").touch()
    result = subprocess.run(
        ["bash", "-e", "-c", steps[index]["run"]],
        env={**os.environ, "TEST_PATHS": paths},
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "does not exist" in result.stdout
    assert index < next(i for i, step in enumerate(steps) if step.get("uses", "").startswith("actions/setup-python@"))


@pytest.mark.parametrize("paths", ["tests/missing.py tests/other.py", "tests/existing.py::test_case tests/missing.py"])
def test_focused_proof_names_every_missing_path_before_setup(paths, tmp_path):
    steps = _workflow()["jobs"]["tests"]["steps"]
    index = next(i for i, step in enumerate(steps) if step.get("name") == "Require explicit test paths")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/existing.py").touch()
    result = subprocess.run(
        ["bash", "-e", "-c", steps[index]["run"]],
        env={**os.environ, "TEST_PATHS": paths},
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    for path in paths.split():
        if "existing" not in path:
            assert f"Test path {path} does not exist." in result.stdout
    assert index < next(i for i, step in enumerate(steps) if step.get("uses", "").startswith("actions/setup-python@"))
    assert index < next(i for i, step in enumerate(steps) if step.get("name") == "Install dependencies")


@pytest.mark.parametrize(("paths", "code"), [("tests/existing.py", 0), ("tests/missing.py", 1), ("", 1)])
def test_selected_unit_paths_are_checked_before_setup(paths, code, tmp_path):
    jobs = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    steps = jobs["unit-selected"]["steps"]
    index = next(i for i, step in enumerate(steps) if step.get("name") == "Require selected test paths")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/existing.py").touch()
    result = subprocess.run(
        ["bash", "-e", "-c", steps[index]["run"]],
        env={**os.environ, "FILES": paths, "LEG": "1"},
        cwd=tmp_path,
        capture_output=True,
    )
    assert result.returncode == code, result.stdout + result.stderr
    assert index < next(i for i, step in enumerate(steps) if step.get("uses", "").startswith("actions/setup-python@"))


@pytest.mark.parametrize(
    ("paths", "existing_only", "code", "selected"),
    [
        ("tests/existing.py::test_case", "false", 0, None),
        ("tests/missing.py tests/existing.py::test_case tests/other.py", "true", 0, "tests/existing.py::test_case"),
        ("tests/missing.py tests/other.py", "true", 1, None),
        ("tests/existing.py tests/missing.py", "false", 1, None),
        ("tests/existing.py hooks/missing.py", "true", 1, None),
    ],
)
def test_focused_proof_runs_only_existing_requests_when_explicitly_asked(
    paths, existing_only, code, selected, tmp_path
):
    step = next(s for s in _workflow()["jobs"]["tests"]["steps"] if s.get("name") == "Require explicit test paths")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/existing.py").touch()
    output = tmp_path / "github-env"
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env={**os.environ, "TEST_PATHS": paths, "EXISTING_ONLY": existing_only, "GITHUB_ENV": str(output)},
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == code, result.stdout + result.stderr
    if selected is None:
        assert not output.exists()
    else:
        assert output.read_text() == f"TEST_PATHS={selected}\n"
        assert "::warning::Test path tests/missing.py does not exist." in result.stdout
        assert "::warning::Test path tests/other.py does not exist." in result.stdout


def test_selected_queue_proof_names_every_missing_path_before_setup(tmp_path):
    jobs = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    step = next(s for s in jobs["unit-selected"]["steps"] if s.get("name") == "Require selected test paths")
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env={**os.environ, "FILES": "tests/missing.py tests/other.py", "LEG": "1"},
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "Selected test path tests/missing.py does not exist." in result.stdout
    assert "Selected test path tests/other.py does not exist." in result.stdout


def test_proofs_reuse_selected_mutation_and_smoke_execution():
    workflow = _workflow()
    jobs = workflow["jobs"]
    for proof, reusable in (
        ("mutation", "mutation-preflight"),
        ("semgrep", "semgrep"),
        ("worker-image", "swarm-node-smoke"),
        ("swarm-image", "swarm-smoke"),
        ("brain-smoke", "brain-smoke"),
        ("helm-kind", "helm-kind"),
    ):
        job = jobs[proof]
        assert job["uses"] == f"./.github/workflows/{reusable}.yml"
        assert job["if"] == f"inputs.proof == '{proof}'"
        assert "steps" not in job
        assert "needs" not in job
        assert not job.get("continue-on-error", False)
        if proof in {"mutation", "semgrep"}:
            assert job["with"] == {"base": "${{ inputs.base }}"}
    callable = yaml.safe_load((_ROOT / ".github/workflows/mutation-preflight.yml").read_text())
    assert callable[True]["workflow_call"]["inputs"]["base"] == {"type": "string", "required": True}


def test_a_nested_report_with_workflow_command_text_stays_out_of_the_log(tmp_path):
    planted = "tests/test_plant.py::test_case[##[error]planted]"
    junit = tmp_path / "source-7.xml"
    junit.write_text(
        '<testsuites><testsuite tests="3" failures="1" errors="0" skipped="1">'
        f'<testcase name="{planted}" time="1.5"><failure message="##[error]planted"/></testcase>'
        "</testsuite></testsuites>"
    )
    result = subprocess.CompletedProcess([], 1, stdout=f"FAILED {planted}\n", stderr="::error::nested\n")
    summary = nested_report(result, junit)
    assert summary == (
        "nested pytest exited 1; tests 3 failures 1 errors 0 skipped 1; "
        f"read source-7.xml and source-7.log in the {REPORT_ARTIFACT} artifact"
    )
    kept = (tmp_path / "source-7.log").read_text() == f"FAILED {planted}\n::error::nested\n"
    assert kept, "the report log lost the nested output"
    assert nested_report(subprocess.CompletedProcess([], 2, stdout="", stderr=""), tmp_path / "missing.xml") == (
        f"nested pytest exited 2; no junit report; read missing.xml and missing.log in the {REPORT_ARTIFACT} artifact"
    )


def test_focused_proofs_upload_every_nested_report():
    steps = _workflow()["jobs"]["tests"]["steps"]
    run = next(s for s in steps if s.get("name") == "Run selected tests")
    upload = next(s for s in steps if s.get("name") == "Retain nested proof reports")
    assert steps.index(upload) == steps.index(run) + 1
    assert run["env"]["PROOF_REPORT_DIR"] == "${{ runner.temp }}/proof-reports"
    assert upload["if"] == "always()"
    assert upload["uses"] == "actions/upload-artifact@v4"
    assert upload["with"]["name"] == REPORT_ARTIFACT
    assert upload["with"]["path"] == run["env"]["PROOF_REPORT_DIR"]


def test_the_sandbox_proof_groups_share_the_suite_evenly():
    from pytest_split.algorithms import Algorithms

    durations = json.loads((_ROOT / ".test_durations").read_text())
    untimed = [f"tests/test_untimed.py::test_{index}" for index in range(len(durations) // 20)]
    items = [SimpleNamespace(nodeid=nodeid) for nodeid in [*durations, *untimed]]
    groups = Algorithms[SPLIT_ALGORITHM].value(8, items, durations)
    counts = [len(group.selected) for group in groups]
    seconds = [group.duration for group in groups]
    assert max(counts) <= 1.5 * min(counts), counts
    assert max(seconds) <= 1.1 * sum(seconds) / len(seconds), seconds


def test_an_untimed_test_counts_as_its_own_file_mean():
    from tests.split_fallback import file_mean_durations

    durations = {
        "tests/test_slow.py::test_a": 30.0,
        "tests/test_slow.py::test_b": 50.0,
        "tests/test_fast.py::test_a": 0.1,
    }
    nodeids = [*durations, "tests/test_slow.py::test_new", "tests/test_other.py::test_new"]
    filled = file_mean_durations(nodeids, durations)
    assert filled["tests/test_slow.py::test_new"] == 40.0
    assert "tests/test_other.py::test_new" not in filled
    assert {nodeid: filled[nodeid] for nodeid in durations} == durations


def test_the_sandbox_proof_loads_the_file_mean_fallback():
    import ast

    tree = ast.parse((_ROOT / "tests/mutation_sandbox_input_proof.py").read_text())
    pairs = {
        (first.value, second.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.List)
        for first, second in zip(node.elts, node.elts[1:])
        if isinstance(first, ast.Constant) and isinstance(second, ast.Constant)
    }
    assert ("-p", "tests.split_fallback") in pairs
