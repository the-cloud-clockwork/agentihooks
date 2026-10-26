import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit


def _jobs():
    root = Path(__file__).resolve().parents[1]
    return yaml.safe_load((root / ".github/workflows/test.yml").read_text())["jobs"]


def test_shard_graders_check_out_event_base_before_installing():
    steps = _jobs()["shard-check"]["steps"]
    checkout = steps[0]
    assert checkout["uses"] == "actions/checkout@v4"
    assert checkout["with"]["ref"] == (
        "${{ github.event_name == 'pull_request' && github.base_ref || github.event.merge_group.base_sha || "
        "github.event_name == 'push' && github.sha || github.event.before || "
        "(inputs.base == 'origin/dev' && 'dev' || inputs.base) }}"
    )
    assert checkout["with"]["persist-credentials"] is False
    assert sum(step.get("uses", "").startswith("actions/checkout@") for step in steps) == 1


def test_shard_grader_never_restores_an_environment_written_by_head_tests():
    steps = _jobs()["shard-check"]["steps"]
    assert not any(step.get("uses", "").startswith("actions/cache") for step in steps)
    installs = [step for step in steps if step.get("name") == "Install grader dependencies"]
    assert not installs or [_as_python(step["run"]) for step in installs] == ['python -m pip install "pytest>=8.0"']
    assert not any(step.get("name") == "Use the test environment" for step in steps)


def test_head_collection_is_uploaded_once_per_interpreter_and_downloaded_as_data():
    jobs = _jobs()
    split = jobs["split"]["steps"]
    collect = next(step for step in split if step.get("name") == "Adopt latest dev durations")
    upload = next(step for step in split if step.get("name") == "Upload collected tests")
    assert "if" not in upload
    assert collect["run"].endswith(" --collected collected.json")
    assert "--shard" not in collect["run"]
    versions = jobs["split"]["strategy"]["matrix"]["python-version"]
    assert jobs["test-count"]["strategy"]["matrix"]["python-version"] == ["3.12"]
    assert jobs["unit"]["strategy"]["matrix"]["python-version"] == ["3.12"]
    assert versions == ["3.12"]
    assert upload["with"]["name"] == "collected-${{ matrix.python-version }}"
    assert upload["with"]["path"] == "collected.json"
    assert upload["with"]["if-no-files-found"] == "error"
    assert split.index(collect) < split.index(upload)
    steps = jobs["shard-check"]["steps"]
    download = next(step for step in steps if step.get("name") == "Download collected tests")
    check = next(step for step in steps if _as_python(step.get("run", "")).startswith("python -m tests.shard_check"))
    if "name" in download["with"]:
        assert download["with"]["name"] == upload["with"]["name"]
        assert f"--collected {download['with']['path']}/collected.json" in check["run"]
    else:
        assert "--collected collected-data/collected.json" in check["run"]
        assert download["with"] == {
            "pattern": "{collected,reused-tests}-${{ matrix.python-version }}",
            "path": "shard-data",
        }
        layout = next(step for step in steps if step.get("name") == "Arrange shard verification data")
        assert 'mv "shard-data/collected-$PYTHON_VERSION" collected-data' in layout["run"]
        assert steps.index(download) < steps.index(layout) < steps.index(check)
    assert steps.index(download) < steps.index(check)


def test_shard_grading_keeps_head_data_separate_from_base_modules():
    steps = _jobs()["shard-check"]["steps"]
    commands = [_as_python(step.get("run", "")) for step in steps]
    commands = [command for command in commands if command.startswith("python -m tests.shard_")]
    commands = [
        command.replace("shard-reports/durations-*", "shard-durations/*").replace(
            "shard-reports/flakes-*", "shard-flakes/*"
        )
        for command in commands
    ]
    assert commands == [
        "python -m tests.shard_check shard-durations/*/durations.json --collected collected-data/collected.json $REUSED",
        'python -m tests.shard_summary --version "$PYTHON_VERSION" --durations shard-durations/*/durations.json \\\n'
        '  --collected collected-data/collected.json --plan "$PLAN" --flakes shard-flakes/*/flakes.jsonl'
        ' | tee -a "$GITHUB_STEP_SUMMARY"\n',
        "python -m tests.shard_budget $REPORT shard-durations/*/durations.json",
    ]


def test_shard_grading_has_no_legacy_grader_and_no_base_branch_conditions():
    jobs = _jobs()
    steps = jobs["shard-check"]["steps"]
    names = [step.get("name", "") for step in steps]
    assert not any("legacy" in name for name in names)
    assert not any(step.get("uses", "").startswith("astral-sh/setup-uv") for step in steps)
    for job in ("split", "unit", "shard-check"):
        assert "base.ref" not in str(jobs[job])
        assert "'main'" not in str(jobs[job])


_ACTIONS_SHELLS = {None: ["bash", "-e"], "bash": ["bash", "--noprofile", "--norc", "-eo", "pipefail"]}


def _as_python(command):
    return re.sub(r"^python3 ", "python ", command)


def _run_summary_step(tmp_path, python_body, interpreter="python"):
    step = next(s for s in _jobs()["shard-check"]["steps"] if s.get("name") == "Summarize reused, ran and flaky tests")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "python"
    fake.write_text(f"#!/usr/bin/env bash\n{python_body}\n")
    fake.chmod(0o755)
    (bin_dir / "python3").symlink_to(fake)
    script = tmp_path / "step.sh"
    script.write_text(re.sub(r"^python3? ", f"{interpreter} ", step["run"], flags=re.M))
    summary = tmp_path / "summary.md"
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GITHUB_STEP_SUMMARY": str(summary),
        "PYTHON_VERSION": "3.12",
        "PLAN": "plan.json",
    }
    result = subprocess.run(
        [*_ACTIONS_SHELLS[step.get("shell")], str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result, summary.read_text() if summary.exists() else ""


@pytest.mark.parametrize("interpreter", ["python", "python3"])
def test_shard_summary_step_goes_red_when_the_summary_command_crashes(tmp_path, interpreter):
    result, _ = _run_summary_step(tmp_path, "echo 'No module named tests.shard_summary' >&2\nexit 1", interpreter)
    assert result.returncode != 0
    assert "No module named tests.shard_summary" in result.stderr


@pytest.mark.parametrize("interpreter", ["python", "python3"])
def test_shard_summary_step_passes_and_writes_a_normal_summary(tmp_path, interpreter):
    result, summary = _run_summary_step(tmp_path, "echo 'Ran 12 tests'", interpreter)
    assert result.returncode == 0
    assert "Ran 12 tests" in result.stdout
    assert "Ran 12 tests" in summary


@pytest.mark.parametrize("case", ["complete", "missing", "duplicate", "reused", "missing_collection", "missing_plan"])
def test_grouped_download_preserves_shard_integrity_without_installed_dependencies(tmp_path, case):
    steps = _jobs()["shard-check"]["steps"]
    downloads = [step for step in steps if step.get("uses", "").startswith("actions/download-artifact@")]
    assert [download["with"] for download in downloads] == [
        {"pattern": "{collected,reused-tests}-${{ matrix.python-version }}", "path": "shard-data"},
        {"pattern": "{collected,reused-tests}-${{ matrix.python-version }}", "path": "shard-data"},
        {"pattern": "{durations,flakes}-${{ matrix.python-version }}-*", "path": "shard-reports"},
        {"pattern": "{durations,flakes}-${{ matrix.python-version }}-*", "path": "shard-reports"},
    ]
    for first, retry in ((downloads[0], downloads[1]), (downloads[2], downloads[3])):
        assert first["continue-on-error"] is True
        assert retry["if"] == f"steps.{first['id']}.outcome == 'failure'"
        assert "continue-on-error" not in retry
        assert "if" not in first
        wait = steps[steps.index(retry) - 1]
        assert wait["if"] == retry["if"]
        assert wait["run"] == "sleep 30"
    require = next(step for step in steps if step.get("name") == "Require every planned shard report")
    assert steps.index(downloads[2]) < steps.index(downloads[3]) < steps.index(require)
    assert not any(step.get("name") == "Install grader dependencies" for step in steps)
    prefetch = next(step for step in steps if "python -m scripts.ci_gate_shards" in step.get("run", ""))
    layout = next(step for step in steps if step.get("name") == "Arrange shard verification data")
    assert steps.index(layout) < steps.index(prefetch) < steps.index(downloads[1])
    root = tmp_path / "shard-data"
    reports = tmp_path / "shard-reports"
    for index, nodeids in enumerate(
        (["suite::a"], ["suite::a", "suite::b"] if case == "duplicate" else ["suite::b"]), 1
    ):
        if case == "missing" and index == 2:
            continue
        directory = reports / f"durations-3.12-{index}"
        directory.mkdir(parents=True)
        (directory / "durations.json").write_text(json.dumps(dict.fromkeys(nodeids, 0.1)))
        (directory / "durations.sha256").write_text("a" * 64)
    root.mkdir()
    if case != "missing_collection":
        directory = root / "collected-3.12"
        directory.mkdir()
        collected = ["suite::a", "suite::b", *(["suite::reused"] if case == "reused" else [])]
        (directory / "collected.json").write_text(json.dumps(collected))
    if case == "reused":
        directory = root / "reused-tests-3.12"
        directory.mkdir()
        (directory / "reused-tests.json").write_text(json.dumps({"tests": ["suite::reused"]}))
    arranged = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", layout["run"]],
        cwd=tmp_path,
        env={**os.environ, "PYTHON_VERSION": "3.12", "REUSED": "1" if case in {"reused", "missing_plan"} else "0"},
        capture_output=True,
        text=True,
    )
    if case in {"missing_collection", "missing_plan"}:
        assert arranged.returncode != 0
        return
    assert arranged.returncode == 0, arranged.stdout + arranged.stderr
    command = [
        sys.executable,
        "-S",
        "-m",
        "tests.shard_check",
        *(str(path) for path in sorted(reports.glob("durations-*/durations.json"))),
        "--collected",
        str(tmp_path / "collected-data/collected.json"),
    ]
    if case == "reused":
        command.extend(["--reused", str(tmp_path / "reused-plan/reused-tests.json")])
    result = subprocess.run(command, cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
    assert result.returncode == (1 if case in {"missing", "duplicate"} else 0), result.stdout + result.stderr
    if case == "missing":
        assert "suite::b ran 0 times" in result.stdout
    elif case == "duplicate":
        assert "suite::a ran 2 times" in result.stdout
    else:
        assert "0 ran zero times or more than once" in result.stdout


@pytest.mark.parametrize("module", ["shard_check", "shard_summary", "shard_budget"])
def test_shard_verifier_commands_run_with_only_system_python3(tmp_path, module):
    step = next(step for step in _jobs()["shard-check"]["steps"] if f"-m tests.{module} " in step.get("run", ""))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python3").symlink_to(sys.executable)
    (bin_dir / "tee").symlink_to("/usr/bin/tee")
    durations = tmp_path / "shard-reports/durations-3.12-1"
    durations.mkdir(parents=True)
    (durations / "durations.json").write_text(json.dumps({"suite::a": 0.1}))
    (durations / "durations.sha256").write_text("a" * 64)
    collection = tmp_path / "collected-data"
    collection.mkdir()
    (collection / "collected.json").write_text(json.dumps(["suite::a"]))
    flakes = tmp_path / "shard-reports/flakes-3.12-1"
    flakes.mkdir(parents=True)
    (flakes / "flakes.jsonl").write_text("")
    result = subprocess.run(
        ["/bin/bash", "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=tmp_path,
        env={
            "PATH": str(bin_dir),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
            "PYTHON_VERSION": "3.12",
            "REUSED": "",
            "PLAN": "reused-plan/reused-tests.json",
            "REPORT": "",
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("missing", [None, "durations-3.12-1", "flakes-3.12-2"])
def test_downloaded_reports_check_needs_no_new_base_grader(tmp_path, missing):
    step = next(
        step for step in _jobs()["shard-check"]["steps"] if step.get("name") == "Require every planned shard report"
    )
    for kind in ("durations", "flakes"):
        for shard in (1, 2):
            name = f"{kind}-3.12-{shard}"
            if name != missing:
                (tmp_path / "shard-reports" / name).mkdir(parents=True)
    result = subprocess.run(
        ["/bin/bash", "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=tmp_path,
        env={"PATH": os.defpath, "SHARDS": "[1,2]"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert (result.returncode == 0) == (missing is None), result.stdout + result.stderr
    if missing:
        assert missing in result.stdout + result.stderr


def test_a_downloaded_report_that_is_a_file_is_still_missing(tmp_path):
    step = next(
        step for step in _jobs()["shard-check"]["steps"] if step.get("name") == "Require every planned shard report"
    )
    reports = tmp_path / "shard-reports"
    reports.mkdir()
    (reports / "durations-3.12-4").mkdir()
    (reports / "flakes-3.12-4").write_text("not a report folder")
    result = subprocess.run(
        ["/bin/bash", "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=tmp_path,
        env={"PATH": os.defpath, "SHARDS": "[4]"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1
    assert "Missing shard reports: flakes-3.12-4" in result.stdout
