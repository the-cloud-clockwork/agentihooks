import signal
import subprocess
import sys
import time

import pytest

from scripts.ci_mutation.runner import run_gate, run_process


def test_over_budget_reports_every_unfinished_file_by_name(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("scripts.ci_mutation.runner.time.monotonic", lambda: 10)
    changes = {"hooks/slow.py": {1}, "scripts/next.py": {2}}
    report = run_gate(tmp_path, changes, tmp_path / "output", 0)
    assert report["failed"]
    assert report["not_mutated"] == [
        {"path": "hooks/slow.py", "reason": "over budget"},
        {"path": "scripts/next.py", "reason": "over budget"},
    ]
    assert "hooks/slow.py: not mutated, over budget" in capsys.readouterr().out


def test_missing_tests_fails_closed(tmp_path):
    (tmp_path / "tests").mkdir()
    report = run_gate(tmp_path, {"hooks/unknown.py": {1}}, tmp_path / "output", 60)
    assert report["failed"]
    assert report["not_mutated"] == [{"path": "hooks/unknown.py", "reason": "no matching or importing test modules"}]


def test_process_timeout_returns_no_status_and_records_output(tmp_path, monkeypatch):
    log = tmp_path / "process.log"
    wait = subprocess.Popen.wait
    started = []

    def wait_for_output(process, timeout=None):
        started.append(process)
        deadline = time.monotonic() + 10
        while timeout is not None and not log.read_text() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        return wait(process, timeout)

    monkeypatch.setattr(subprocess.Popen, "wait", wait_for_output)
    code = "import time; time.sleep(0.5); print('started', flush=True); time.sleep(60)"
    assert run_process([sys.executable, "-c", code], tmp_path, 0.2, log) is None
    assert log.read_text() == "started\n"
    assert started[0].returncode == -signal.SIGKILL


def test_process_exit_status_is_preserved(tmp_path):
    assert run_process([sys.executable, "-c", "raise SystemExit(7)"], tmp_path, 10, tmp_path / "log") == 7


def test_process_runs_in_requested_directory_and_captures_stderr(tmp_path):
    log = tmp_path / "process.log"
    command = [
        sys.executable,
        "-c",
        "from pathlib import Path; import sys; print(Path.cwd()); print('error', file=sys.stderr)",
    ]
    assert run_process(command, tmp_path, 10, log) == 0
    assert sorted(log.read_text().splitlines()) == sorted([str(tmp_path), "error"])


def test_process_writes_no_bytecode(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    log = tmp_path / "process.log"
    code = "import os, sys; print(sys.dont_write_bytecode, os.environ['PYTHONDONTWRITEBYTECODE'])"
    assert run_process([sys.executable, "-c", code], tmp_path, 10, log) == 0
    assert log.read_text() == "True 1\n"


def _two_groups(tmp_path):
    for path in ("hooks/sample.py", "scripts/ci_mutation/identity.py"):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text("def f():\n    return 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_sample.py").write_text("pass\n")
    (tmp_path / "tests/test_identity.py").write_text("pass\n")
    return {"hooks/sample.py": {2}, "scripts/ci_mutation/identity.py": {2}}


def test_every_group_reached_after_the_deadline_is_reported_over_budget(tmp_path, monkeypatch, capsys):
    clock = iter([0, 9, 9, 10, 10])
    monkeypatch.setattr("scripts.ci_mutation.runner.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("scripts.ci_mutation.runner.mutate_files", lambda *args: pytest.fail("ran past budget"))
    report = run_gate(tmp_path, _two_groups(tmp_path), tmp_path / "output", 10)
    assert report["failed"]
    assert report["not_mutated"] == [
        {"path": "hooks/sample.py", "reason": "over budget"},
        {"path": "scripts/ci_mutation/identity.py", "reason": "over budget"},
    ]
    assert "hooks/sample.py: not mutated, over budget" in capsys.readouterr().out


def test_a_failed_group_names_its_reason_for_every_file_and_later_groups_still_run(tmp_path, monkeypatch):
    calls = []

    def mutate(root, work, selected, deadline):
        calls.append(list(selected))
        if "hooks/sample.py" in selected:
            return {}, "mutmut failed with exit 1"
        return {path: [] for path in selected}, ""

    monkeypatch.setattr("scripts.ci_mutation.runner.mutate_files", mutate)
    report = run_gate(tmp_path, _two_groups(tmp_path), tmp_path / "output", 60)
    assert calls == [["hooks/sample.py"], ["scripts/ci_mutation/identity.py"]]
    assert report["not_mutated"] == [{"path": "hooks/sample.py", "reason": "mutmut failed with exit 1"}]
    assert [result["path"] for result in report["files"]] == ["scripts/ci_mutation/identity.py"]


def test_empty_scope_passes_without_mutmut(tmp_path):
    report = run_gate(tmp_path, {}, tmp_path / "output", 60)
    assert report == {"files": [], "not_mutated": [], "failed": False}
    assert (tmp_path / "output" / "report.json").exists()


def test_module_without_functions_is_reported_and_passes(tmp_path, capsys):
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks/__init__.py").write_text("")
    report = run_gate(tmp_path, {"hooks/__init__.py": set()}, tmp_path / "output", 60)
    assert report["failed"] is False
    assert report["files"][0]["counts"] == {}
    assert "hooks/__init__.py: no mutable functions" in capsys.readouterr().out


def test_existing_nested_output_folder_and_multiple_empty_modules_pass(tmp_path):
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks/one.py").write_text("")
    (tmp_path / "hooks/two.py").write_text("")
    output = tmp_path / "nested/output"
    report = run_gate(tmp_path, {"hooks/one.py": set(), "hooks/two.py": set()}, output, 60)
    assert report["files"] == [
        {"path": "hooks/one.py", "counts": {}, "failures": [], "untouched_survivors": [], "cleared": []},
        {"path": "hooks/two.py", "counts": {}, "failures": [], "untouched_survivors": [], "cleared": []},
    ]
    assert run_gate(tmp_path, {}, output, 60)["failed"] is False


def test_workspace_scopes_mutmut_and_preserves_the_pytest_config(tmp_path):
    import tomllib

    from scripts.ci_mutation.runner import prepare_workspace

    root = tmp_path / "repo"
    root.mkdir()
    (root / "pyproject.toml").write_text('[tool.pytest.ini_options]\nasyncio_mode="auto"\n')
    for name in (
        "hooks",
        "scripts",
        "tests",
        "profiles",
        "docs",
        ".github",
        "evidence",
        "docker/swarm-node",
        ".agentihooks/conditions",
    ):
        (root / name).mkdir(parents=True)
        (root / name / "asset.txt").write_text(name)
    (root / "Swarm-v2.md").write_text("# plan\n")
    (root / "hooks" / "__pycache__").mkdir()
    (root / "hooks" / "__pycache__" / "old.pyc").write_bytes(b"old")
    (root / "hooks" / "old.pyc").write_bytes(b"old")
    (root / ".test_durations").write_text('{"tests/test_sample.py::t": 1.5}')
    work = tmp_path / "nested" / "work"
    work.mkdir(parents=True)
    prepare_workspace(root, work, ["hooks/sample.py", "scripts/other.py"], ["tests/test_sample.py"])
    config = tomllib.loads((work / "pyproject.toml").read_text())
    assert config["tool"]["pytest"]["ini_options"] == {"asyncio_mode": "auto"}
    assert config["tool"]["mutmut"]["source_paths"] == ["hooks/", "scripts/"]
    assert config["tool"]["mutmut"]["only_mutate"] == ["hooks/sample.py", "scripts/other.py"]
    assert config["tool"]["mutmut"]["pytest_add_cli_args_test_selection"] == ["tests/test_sample.py"]
    assert config["tool"]["mutmut"]["also_copy"] == [
        "profiles/",
        "docs/",
        ".github/",
        "evidence/",
        "Swarm-v2.md",
        "docker/swarm-node/",
        ".agentihooks/conditions/",
    ]
    assert config["tool"]["mutmut"]["pytest_add_cli_args"] == [
        "-q",
        "-x",
        "-o",
        "addopts=",
        "-p",
        "pytest_asyncio.plugin",
        "-p",
        "scripts.ci_mutation.identity",
        "--mutated-path=hooks/sample.py",
        "--mutated-path=scripts/other.py",
    ]
    for name in (
        "hooks",
        "scripts",
        "tests",
        "profiles",
        "docs",
        ".github",
        "evidence",
        "docker/swarm-node",
        ".agentihooks/conditions",
    ):
        assert (work / name / "asset.txt").read_text() == name
    assert (work / "Swarm-v2.md").read_text() == "# plan\n"
    assert not (work / "hooks/__pycache__").exists()
    assert not (work / "hooks/old.pyc").exists()
    assert (work / ".test_durations").read_text() == '{"tests/test_sample.py::t": 1.5}'


def test_workspace_inside_a_copied_folder_is_not_copied_into_itself(tmp_path):
    from scripts.ci_mutation.runner import prepare_workspace

    root = tmp_path / "repo"
    (root / "evidence").mkdir(parents=True)
    (root / "evidence" / "result.json").write_text("{}")
    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    work = root / "evidence" / "0-work"
    work.mkdir()
    prepare_workspace(root, work, ["scripts/other.py"], ["tests/test_sample.py"])
    assert sorted(p.name for p in (work / "evidence").iterdir()) == ["result.json"]


def test_workspace_mutating_the_identity_plugin_does_not_load_its_mutated_copy(tmp_path):
    import tomllib

    from scripts.ci_mutation.runner import prepare_workspace

    root = tmp_path / "repo"
    root.mkdir()
    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    work = tmp_path / "work"
    work.mkdir()
    prepare_workspace(root, work, ["scripts/ci_mutation/identity.py"], ["tests/test_ci_mutation_identity.py"])
    assert not (work / ".test_durations").exists()
    assert not (work / "Swarm-v2.md").exists()
    args = tomllib.loads((work / "pyproject.toml").read_text())["tool"]["mutmut"]["pytest_add_cli_args"]
    assert args == ["-q", "-x", "-o", "addopts=", "-p", "pytest_asyncio.plugin"]


@pytest.mark.parametrize(
    "statuses,reason",
    [
        ([None], "over budget"),
        ([7], "mutmut failed with exit 7"),
        ([0, None], "over budget"),
        ([0, 7], "report failed with exit 7"),
        ([0, 0], ""),
    ],
)
def test_external_mutation_run_failures_and_results_are_preserved(tmp_path, monkeypatch, statuses, reason):
    import json

    from scripts.ci_mutation.runner import mutate_files

    commands = []
    selected = {"hooks/sample.py": ({2}, ["tests/test_sample.py"]), "scripts/other.py": ({4, 3}, ["tests/test_o.py"])}

    def prepare(root, work, paths, tests):
        assert (root, work) == (tmp_path, tmp_path / "work")
        assert paths == ["hooks/sample.py", "scripts/other.py"]
        assert tests == ["tests/test_o.py", "tests/test_sample.py"]
        work.mkdir()

    def process(command, cwd, timeout, log):
        commands.append(command)
        assert cwd == tmp_path / "work"
        assert timeout == 10
        assert log == cwd / ("run.log" if len(commands) == 1 else "report.log")
        if len(commands) == 2:
            (cwd / "results.json").write_text(json.dumps({"hooks/sample.py": [{"status": "killed"}]}))
        return statuses[len(commands) - 1]

    monkeypatch.setattr("scripts.ci_mutation.runner.prepare_workspace", prepare)
    monkeypatch.setattr("scripts.ci_mutation.runner.run_process", process)
    monkeypatch.setattr("scripts.ci_mutation.runner.time.monotonic", lambda: 10)
    rows, error = mutate_files(tmp_path, tmp_path / "work", selected, 20)
    expected = reason
    if reason.startswith("mutmut failed"):
        expected += f"; see {tmp_path / 'work/run.log'}"
    if reason.startswith("report failed"):
        expected += f"; see {tmp_path / 'work/report.log'}"
    assert error == expected
    assert rows == ({"hooks/sample.py": [{"status": "killed"}]} if reason == "" else {})
    assert commands[0] == [
        sys.executable,
        "-m",
        "scripts.ci_mutation.selection",
        str(tmp_path / "work/changed-lines.json"),
    ]
    assert json.loads((tmp_path / "work/changed-lines.json").read_text()) == {
        "hooks/sample.py": {"lines": [2], "tests": ["tests/test_sample.py"]},
        "scripts/other.py": {"lines": [3, 4], "tests": ["tests/test_o.py"]},
    }
    if len(commands) == 2:
        assert commands[1] == [
            sys.executable,
            "-m",
            "scripts.ci_mutation.report",
            str(tmp_path / "work/results.json"),
            "hooks/sample.py",
            "scripts/other.py",
        ]


@pytest.mark.parametrize(
    "changed,clearance,fails", [(2, False, True), (3, False, False), (2, True, False), (2, "folder", False)]
)
def test_gate_persists_full_mutation_evidence_and_respects_reader_clearance(
    tmp_path, monkeypatch, capsys, changed, clearance, fails
):
    import json

    (tmp_path / "hooks").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "hooks/sample.py").write_text("def f():\n    return 1\n")
    (tmp_path / "tests/test_sample.py").write_text("pass\n")
    row = {"name": "hooks.sample.x_f__mutmut_1", "status": "survived", "lines": [2], "fingerprint": "abc"}

    def mutate(root, work, selected, deadline):
        assert root == tmp_path
        assert work.parent == tmp_path / "output"
        assert work.name.startswith("0-")
        assert work.is_dir()
        assert selected == {"hooks/sample.py": ({changed}, ["tests/test_sample.py"])}
        assert deadline > 0
        return {"hooks/sample.py": [row]}, ""

    monkeypatch.setattr("scripts.ci_mutation.runner.mutate_files", mutate)
    if clearance:
        target = tmp_path / "mutation-cleared.txt"
        if clearance == "folder":
            import hashlib

            key = "hooks/sample.py:hooks.sample.x_f__mutmut_1:abc"
            target = tmp_path / "mutation-clearances" / f"{hashlib.sha256(key.encode()).hexdigest()}.json"
            target.parent.mkdir()
        target.write_text(
            json.dumps(
                {
                    "hooks/sample.py:hooks.sample.x_f__mutmut_1:abc": {
                        "reader": "Standards",
                        "reason": "Only an unobserved message changes",
                    }
                }
            )
        )
    report = run_gate(tmp_path, {"hooks/sample.py": {changed}}, tmp_path / "output", 60)
    assert report["failed"] is fails
    assert report["files"][0]["counts"] == {"survived": 1}
    assert report["not_mutated"] == []
    assert json.loads((tmp_path / "output/report.json").read_text()) == report
    assert json.loads(capsys.readouterr().out) == report["files"][0]
