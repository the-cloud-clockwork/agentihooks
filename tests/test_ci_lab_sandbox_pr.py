import os
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
PREPARE = "Let bubblewrap create user and network namespaces"
RUN = "Run the lab sandbox tests under real bubblewrap"
SHELL = ["/bin/bash", "--noprofile", "--norc", "-eo", "pipefail", "-c"]
SYSCTL = "sysctl -w kernel.apparmor_restrict_unprivileged_userns=0"
PROBE = "bwrap --dev-bind / / --unshare-net true"


def workflow(name: str) -> dict:
    return yaml.safe_load((ROOT / f".github/workflows/{name}.yml").read_text())


def lab_step(name: str) -> dict:
    return next(step for step in workflow("writer-lock")["jobs"]["lab-sandbox"]["steps"] if step.get("name") == name)


def fake(folder: Path, name: str, body: str) -> None:
    folder.mkdir(exist_ok=True)
    script = folder / name
    script.write_text(f"#!/bin/sh\n{body}")
    script.chmod(0o755)


@pytest.mark.feature("lab-sandbox-bwrap-pull-request-tier")
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("scripts/ci_lab/sandbox.py", "true"),
        ("scripts/ci_lab/run_static.py", "true"),
        ("scripts/ci_lab/run_tests.py", "true"),
        ("tests/ci_lab/test_sandbox.py", "true"),
        ("tests/ci_lab/test_run_static.py", "true"),
        ("tests/ci_lab/test_run_tests.py", "true"),
        (".github/workflows/writer-lock.yml", "true"),
        ("scripts/ci_lab/worker.py", "false"),
        ("README.md", "false"),
    ],
)
def test_lab_sandbox_selection(path: str, expected: str, tmp_path: Path) -> None:
    step = next(step for step in workflow("writer-lock")["jobs"]["inputs"]["steps"] if step.get("id") == "lab-sandbox")
    assert step["if"] == "github.event_name == 'pull_request'"
    (tmp_path / "lab-sandbox-paths").write_bytes(path.encode() + b"\0")
    fake(
        tmp_path / "bin",
        "git",
        'case "$*" in *merge-base*) echo base ;; *) cat "$RUNNER_TEMP/lab-sandbox-paths" ;; esac\n',
    )
    output = tmp_path / "output"
    subprocess.run(
        [*SHELL, step["run"]],
        env={
            **os.environ,
            "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_OUTPUT": str(output),
            "PR_BASE": "base",
            "PR_HEAD": "head",
            "LAB_SANDBOX_INPUTS": step["env"]["LAB_SANDBOX_INPUTS"],
        },
        check=True,
    )
    assert output.read_text() == f"due={expected}\n"


@pytest.mark.feature("lab-sandbox-bwrap-pull-request-tier")
def test_the_lab_sandbox_job_feeds_the_required_gate_on_selected_pull_requests() -> None:
    tests = workflow("test")["jobs"]
    assert "writer-lock" in tests["gate-required"]["needs"]
    assert tests["writer-lock"]["uses"] == "./.github/workflows/writer-lock.yml"
    assert "github.event_name == 'pull_request'" in tests["writer-lock"]["if"]
    jobs = workflow("writer-lock")["jobs"]
    job = jobs["lab-sandbox"]
    assert job["needs"] == ["inputs"]
    assert job["if"] == (
        "(github.event_name == 'pull_request' && needs.inputs.outputs.lab_sandbox == 'true')"
        " || github.workflow == 'Writer lock'"
    )
    assert job["runs-on"] == "ubuntu-latest"
    assert jobs["inputs"]["outputs"]["lab_sandbox"] == "${{ steps.lab-sandbox.outputs.due }}"
    assert "lab-sandbox" not in jobs["scenario"]["needs"]


@pytest.mark.feature("lab-sandbox-bwrap-pull-request-tier")
@pytest.mark.parametrize(("probe", "code"), [(0, 0), (1, 1)], ids=["isolates", "cannot-isolate"])
def test_the_job_fails_unless_bubblewrap_isolates_after_the_userns_restriction_is_lifted(
    probe: int, code: int, tmp_path: Path
) -> None:
    names = [step.get("name") for step in workflow("writer-lock")["jobs"]["lab-sandbox"]["steps"]]
    assert names.index(PREPARE) < names.index(RUN)
    record = tmp_path / "record"
    fake(tmp_path / "bin", "sudo", 'printf "%s\\n" "$*" >> "$RECORD"\n')
    fake(tmp_path / "bin", "bwrap", f'printf "bwrap %s\\n" "$*" >> "$RECORD"\nexit {probe}\n')
    result = subprocess.run(
        [*SHELL, lab_step(PREPARE)["run"]],
        env={"PATH": str(tmp_path / "bin"), "RECORD": str(record)},
        capture_output=True,
        check=False,
    )
    assert result.returncode == code
    assert record.read_text().splitlines() == [SYSCTL, PROBE]


@pytest.mark.feature("lab-sandbox-bwrap-pull-request-tier")
def test_the_job_installs_bubblewrap_from_the_ubuntu_archive_when_the_runner_lacks_it(tmp_path: Path) -> None:
    record = tmp_path / "record"
    fake(tmp_path / "bin", "sudo", 'printf "%s\\n" "$*" >> "$RECORD"\n')
    result = subprocess.run(
        [*SHELL, lab_step(PREPARE)["run"]],
        env={"PATH": str(tmp_path / "bin"), "RECORD": str(record)},
        capture_output=True,
        check=False,
    )
    update, install, sysctl = record.read_text().splitlines()
    assert update.startswith("apt-get update -qq -o Acquire::Retries=5 -o Dir::Etc::sourcelist=")
    assert install.startswith("apt-get install -y -qq --no-install-recommends -o Acquire::Retries=5")
    assert install.endswith(" -o Dir::Etc::sourceparts=- bubblewrap")
    assert sysctl == SYSCTL
    assert result.returncode == 127


@pytest.mark.feature("lab-sandbox-bwrap-pull-request-tier")
@pytest.mark.parametrize(
    ("output", "status", "code"),
    [
        ("tests/ci_lab/test_sandbox.py::test_a PASSED\n", 0, 0),
        ("tests/ci_lab/test_sandbox.py::test_b SKIPPED (bubblewrap cannot create namespaces on this runner)\n", 0, 1),
        ("tests/ci_lab/test_sandbox.py::test_c FAILED\n", 1, 1),
        ("no tests ran\n", 5, 5),
    ],
    ids=["passed", "skipped", "failed", "none-collected"],
)
def test_the_lab_sandbox_run_fails_on_a_skipped_or_failed_test(
    output: str, status: int, code: int, tmp_path: Path
) -> None:
    step = lab_step(RUN)
    assert step["shell"] == "bash"
    (tmp_path / "pytest-output").write_text(output)
    fake(
        tmp_path / "bin",
        "python",
        f'printf "%s\\n" "$*" > "$RUNNER_TEMP/python-args"\ncat "$RUNNER_TEMP/pytest-output"\nexit {status}\n',
    )
    result = subprocess.run(
        [*SHELL, step["run"]],
        env={**os.environ, "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}", "RUNNER_TEMP": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == code
    assert (tmp_path / "python-args").read_text().split()[:5] == [
        "-m",
        "pytest",
        "tests/ci_lab/test_sandbox.py",
        "tests/ci_lab/test_run_static.py",
        "tests/ci_lab/test_run_tests.py",
    ]
    assert (tmp_path / "lab-sandbox.log").read_text() == output
