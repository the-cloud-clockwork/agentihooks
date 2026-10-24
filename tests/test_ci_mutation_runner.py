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

    def mutate(root, work, selected, deadline, shard, stats):
        assert shard == (0, 1)
        assert stats is None
        calls.append(list(selected))
        if "hooks/sample.py" in selected:
            return {}, "mutmut failed with exit 1"
        return {path: [] for path in selected}, ""

    monkeypatch.setattr("scripts.ci_mutation.runner.mutate_files", mutate)
    report = run_gate(tmp_path, _two_groups(tmp_path), tmp_path / "output", 60)
    assert calls == [["hooks/sample.py"], ["scripts/ci_mutation/identity.py"]]
    assert report["not_mutated"] == [{"path": "hooks/sample.py", "reason": "mutmut failed with exit 1"}]
    assert [result["path"] for result in report["files"]] == ["scripts/ci_mutation/identity.py"]


BASELINE_LOG = """...F
=========================== short test summary info ============================
FAILED tests/test_sample.py::test_gate[ops0-True] - AssertionError: assert 'a' == 'b'
ERROR tests/test_sample.py::test_setup - RuntimeError: no server
RERUN tests/test_sample.py::test_flaky - AssertionError: once
FAILED tests/test_sample.py::test_bare
FAILED tests/test_sample.py::test_spaced[a b] - ValueError: x
FAILED tests/test_sample.py::test_gate[ops0-True] - AssertionError: assert 'a' == 'b'
ERROR tests/test_sample.py::test_gate[ops0-True] - RuntimeError: teardown
1 failed in 3.2s
failed to collect stats. runner returned [1]
"""


def test_a_failed_clean_baseline_names_every_failing_test_in_the_reason_the_red_message_and_the_report(
    tmp_path, monkeypatch, capsys
):
    import json

    logs = iter([BASELINE_LOG.encode() + b"\xff\n", b"FAILED tests/test_identity.py::test_other - KeyError: k\n"])

    def process(command, cwd, timeout, log):
        log.write_bytes(next(logs))
        return 1

    monkeypatch.setattr("scripts.ci_mutation.runner.prepare_workspace", lambda root, work, paths, tests: None)
    monkeypatch.setattr("scripts.ci_mutation.runner.run_process", process)
    report = run_gate(tmp_path, _two_groups(tmp_path), tmp_path / "output", 600)
    first = [
        "tests/test_sample.py::test_gate[ops0-True] - AssertionError: assert 'a' == 'b'",
        "tests/test_sample.py::test_setup - RuntimeError: no server",
        "tests/test_sample.py::test_bare",
        "tests/test_sample.py::test_spaced[a b] - ValueError: x",
    ]
    failing = [*first, "tests/test_identity.py::test_other - KeyError: k"]
    assert report["failing_tests"] == failing
    reason = report["not_mutated"][0]["reason"]
    assert reason.startswith("mutmut failed with exit 1; failing tests: " + "; ".join(first) + "; see ")
    assert f"hooks/sample.py: not mutated, {reason}" in capsys.readouterr().out
    assert json.loads((tmp_path / "output/report.json").read_text())["failing_tests"] == failing


def test_a_clean_baseline_failure_without_a_test_summary_keeps_the_plain_reason(tmp_path, monkeypatch):
    def process(command, cwd, timeout, log):
        log.write_text("Traceback (most recent call last):\nImportError: boom\n")
        return 1

    monkeypatch.setattr("scripts.ci_mutation.runner.prepare_workspace", lambda root, work, paths, tests: None)
    monkeypatch.setattr("scripts.ci_mutation.runner.run_process", process)
    report = run_gate(tmp_path, _two_groups(tmp_path), tmp_path / "output", 600)
    assert "failing_tests" not in report
    assert report["not_mutated"][0]["reason"].startswith("mutmut failed with exit 1; see ")


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
        "observability",
        "docker/swarm-node",
        ".agentihooks/conditions",
        "deploy/helm/agentihooks-swarm/ci",
    ):
        (root / name).mkdir(parents=True)
        (root / name / "asset.txt").write_text(name)
    (root / "Swarm-v2.md").write_text("# plan\n")
    (root / "deploy" / "fixture.yaml").write_text("fixture")
    (root / "tests" / "test_sample.py").write_text('FIXTURE = "deploy/fixture.yaml"\n')
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
    existing_copies = [
        "profiles/",
        "docs/",
        ".github/",
        "evidence/",
        "observability/",
        "docker/swarm-node/",
        ".agentihooks/conditions/",
        "deploy/helm/agentihooks-swarm/ci/",
        ".test_durations",
        "Swarm-v2.md",
    ]
    assert config["tool"]["mutmut"]["also_copy"] in (
        existing_copies,
        [*existing_copies, "deploy/"],
    )
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
        "observability",
        "docker/swarm-node",
        ".agentihooks/conditions",
        "deploy/helm/agentihooks-swarm/ci",
    ):
        assert (work / name / "asset.txt").read_text() == name
    assert (work / "Swarm-v2.md").read_text() == "# plan\n"
    assert not (work / "hooks/__pycache__").exists()
    assert not (work / "hooks/old.pyc").exists()
    assert (work / ".test_durations").read_text() == '{"tests/test_sample.py::t": 1.5}'


def test_workspace_carries_the_repository_root_files_into_the_mutants_copy(tmp_path):
    import tomllib

    from scripts.ci_mutation.runner import prepare_workspace

    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nreadme = "README.md"\n\n[tool.pytest.ini_options]\n')
    copied = ("README.md", "LICENSE", "index.md", "compose.yaml", "sonar-project.properties", ".test_durations")
    for name in (*copied, ".env", ".env.example", ".git"):
        (root / name).write_text(name)
    (tmp_path / "outside.txt").write_text("outside")
    (root / "linked.md").symlink_to(tmp_path / "outside.txt")
    work = tmp_path / "work"
    work.mkdir()
    prepare_workspace(root, work, ["scripts/other.py"], ["tests/test_ci_workflow.py"])
    also_copy = tomllib.loads((work / "pyproject.toml").read_text())["tool"]["mutmut"]["also_copy"]
    assert [name for name in also_copy if not name.endswith("/")] == sorted(copied)
    for name in copied:
        assert (work / name).read_text() == name
    for name in (".env", ".env.example", ".git", "linked.md"):
        assert not (work / name).exists()


@pytest.mark.parametrize(
    "fixture",
    [
        "deploy/chart/ci/smoke.sh",
        "mutation-clearances/reader.json",
        "data/new-fixture.txt",
        "deploy/compose/.env.example",
        "deploy/worker/start.sh",
    ],
)
def test_selected_test_clean_baseline_reads_only_its_extra_repository_fixtures(tmp_path, fixture):
    import os
    import shutil
    import subprocess
    import sys
    import tomllib

    from scripts.ci_mutation.runner import prepare_workspace

    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    resource = root / fixture
    resource.parent.mkdir(parents=True)
    content = "printf 'selected fixture'" if fixture == "deploy/worker/start.sh" else "selected fixture"
    resource.write_text(content)
    (resource.parent / "unrelated.txt").write_text("unrelated")
    (root / "other-data").mkdir()
    (root / "other-data" / "unused.txt").write_text("unused")
    (root / "deploy").mkdir(exist_ok=True)
    (root / "deploy" / "unread.yaml").write_text("unread")
    expression = " / ".join(repr(part) for part in fixture.split("/"))
    read = f"    assert (ROOT / {expression}).read_text() == 'selected fixture'\n"
    if fixture == "deploy/worker/start.sh":
        read = (
            f"    START = ROOT / {expression}\n"
            "    result = subprocess.run(['bash', str(START)], capture_output=True, text=True)\n"
            "    assert result.returncode == 0, result.stderr\n"
            "    assert result.stdout == 'selected fixture'\n"
        )
    (root / "tests/test_fixture.py").write_text(
        "import subprocess\n"
        "from pathlib import Path\n"
        "ROOT = Path(__file__).resolve().parents[1]\n"
        "def test_fixture():\n"
        "    unused_text = 'deploy/unread.yaml'\n"
        "    tmp_path = Path('scratch')\n"
        "    (tmp_path / 'deploy').mkdir(parents=True, exist_ok=True)\n" + read
    )
    (root / "tests/test_unselected.py").write_text('UNUSED = "other-data/unused.txt"\n')
    work = tmp_path / "work"
    work.mkdir()
    prepare_workspace(root, work, ["hooks/sample.py"], ["tests/test_fixture.py"])
    config = tomllib.loads((work / "pyproject.toml").read_text())["tool"]["mutmut"]
    mutants = work / "mutants"
    mutants.mkdir()
    shutil.copytree(work / "tests", mutants / "tests")
    for name in config["also_copy"]:
        source = work / name
        if source.is_dir():
            shutil.copytree(source, mutants / name, dirs_exist_ok=True)
        elif source.exists():
            shutil.copy2(source, mutants / name)
    for baseline in (work, mutants):
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-o", "addopts=", "tests/test_fixture.py"],
            cwd=baseline,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert (baseline / fixture).read_text() == content
        assert not (baseline / resource.relative_to(root).parent / "unrelated.txt").exists()
        assert not (baseline / "other-data").exists()
        assert not (baseline / "deploy/unread.yaml").exists()
    assert f"{fixture.split('/')[0]}/" in config["also_copy"]


@pytest.mark.parametrize(
    "source, expected",
    [
        ('(ROOT / "extra/text.txt").read_bytes()', ["text.txt"]),
        (
            'HERE = Path(__file__).resolve()\nROOT = HERE.parent.parent\n(ROOT / "extra/text.txt").read_text()',
            ["text.txt"],
        ),
        (
            'HERE = Path(__file__)\nROOT = HERE.resolve().parents[1]\n(ROOT / "extra/text.txt").read_text()',
            ["text.txt"],
        ),
        ('PATTERN = "*.json"\nlist((ROOT / "extra").rglob(PATTERN))', ["nested/child.json"]),
        (
            'def ignore(name, value):\n    pass\nignore("ROOT", "unrelated")\n(ROOT / "extra/text.txt").read_text()',
            ["text.txt"],
        ),
        ('(ROOT / "extra/text.txt").exists()', ["text.txt"]),
        ('(ROOT / "extra/text.txt").is_file()', ["text.txt"]),
        ('open(ROOT / "extra/text.txt")', ["text.txt"]),
        ('(ROOT / "extra/text.txt").open()', ["text.txt"]),
        ('subprocess.check_output(["cat", str(ROOT / "extra/text.txt")])', ["text.txt"]),
        ('subprocess.check_call(["cat", str(ROOT / "extra/text.txt")])', ["text.txt"]),
        ('subprocess.call(["cat", str(ROOT / "extra/text.txt")])', ["text.txt"]),
        ('subprocess.Popen(["cat", str(ROOT / "extra/text.txt")])', ["text.txt"]),
        ('list((ROOT / "extra").glob("*.txt"))', ["text.txt"]),
        ('list((ROOT / "extra").rglob("*.json"))', ["nested/child.json"]),
        ('list((ROOT / "extra").iterdir())', ["bytes.bin", "text.txt", "unused.ignore"]),
        (
            'def read(name):\n    return (ROOT / name).read_text()\nread("extra/text.txt")\nread("extra/nested/child.json")',
            ["nested/child.json", "text.txt"],
        ),
        (
            'import pytest\nFIELD = "folder"\n@pytest.mark.parametrize(FIELD, ["extra"])\ndef test_records(folder):\n    return list((ROOT / folder).rglob("*.json"))',
            ["nested/child.json"],
        ),
    ],
)
def test_workspace_discovers_repository_inputs_for_each_selected_read_operation(tmp_path, source, expected):
    from scripts.ci_mutation.runner import prepare_workspace

    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "extra/nested").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    for name in ("text.txt", "bytes.bin", "nested/child.json", "unused.ignore"):
        (root / "extra" / name).write_text(name)
    (root / "tests/test_selected.py").write_text(
        "import subprocess\nfrom pathlib import Path\nROOT = Path(__file__).resolve().parents[1]\n"
        + source
        + "\ndef test_selected():\n    pass\n"
    )
    work = tmp_path / "work"
    work.mkdir()
    prepare_workspace(root, work, ["hooks/sample.py"], ["tests/test_selected.py"])
    copied = sorted(
        path.relative_to(work / "extra").as_posix() for path in (work / "extra").rglob("*") if path.is_file()
    )
    assert copied == expected


def test_workspace_extra_inputs_preserve_templates_and_exclude_credentials_caches_and_links(tmp_path, monkeypatch):
    from pathlib import Path

    from scripts.ci_mutation.runner import prepare_workspace

    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "extra/.git").mkdir(parents=True)
    (root / "extra/__pycache__").mkdir()
    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    templates = [".env.dist", ".env.example", ".env.sample", ".env.template"]
    for name in [
        *templates,
        ".env",
        ".env.local",
        "cache.pyc",
        ".git/index",
        "__pycache__/old.pyc",
        "__pycache__/metadata.json",
        "z-after-link.txt",
    ]:
        (root / "extra" / name).write_text("fixture")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    (root / "extra/linked.txt").symlink_to(outside)
    (root / "tests/test_selected.py").write_text(
        "from pathlib import Path\nROOT = Path(__file__).resolve().parents[1]\n"
        'def test_inputs():\n    return (ROOT / "extra").rglob("*")\n'
    )
    work = tmp_path / "work"
    work.mkdir()
    glob = Path.glob
    monkeypatch.setattr(Path, "glob", lambda path, pattern: iter(sorted(glob(path, pattern))))
    prepare_workspace(root, work, ["hooks/sample.py"], ["tests/test_selected.py::test_inputs"])
    copied = sorted(
        path.relative_to(work / "extra").as_posix() for path in (work / "extra").rglob("*") if path.is_file()
    )
    assert copied == [*templates, "z-after-link.txt"]


def test_workspace_does_not_enumerate_fixture_directories_outside_the_repository(tmp_path, monkeypatch):
    import os
    from pathlib import Path

    from scripts.ci_mutation.runner import prepare_workspace

    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("outside")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    (root / "tests/test_selected.py").write_text(
        "from pathlib import Path\nROOT = Path(__file__).resolve().parents[1]\n"
        'def test_inputs():\n    return (ROOT / "linked").rglob("*")\n'
    )
    scandir = os.scandir

    def scan(path):
        assert not Path(path).resolve().is_relative_to(outside)
        return scandir(path)

    monkeypatch.setattr(os, "scandir", scan)
    work = tmp_path / "work"
    work.mkdir()
    prepare_workspace(root, work, ["hooks/sample.py"], ["tests/test_selected.py"])
    assert not (work / "linked").exists()


def test_workspace_keeps_generated_configuration_and_handles_probed_path_length_limits(tmp_path):
    import tomllib

    from scripts.ci_mutation.runner import prepare_workspace

    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "extra").mkdir()
    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    allowed = "a" * 255
    (root / "extra" / allowed).write_text("fixture")
    rejected = "a" * 256
    (root / "tests/test_selected.py").write_text(
        "from pathlib import Path\nROOT = Path(__file__).resolve().parents[1]\n"
        '(ROOT / "pyproject.toml").read_text()\n'
        f'(ROOT / "extra" / {allowed!r}).read_text()\n'
        "import pytest\n"
        "with pytest.raises(OSError):\n"
        f'    (ROOT / "extra" / {rejected!r}).read_text()\n'
    )
    work = tmp_path / "work"
    work.mkdir()
    prepare_workspace(root, work, ["hooks/sample.py"], ["tests/test_selected.py"])
    config = tomllib.loads((work / "pyproject.toml").read_text())["tool"]["mutmut"]
    assert config["also_copy"][-1] == "extra/"
    assert "pyproject.toml/" not in config["also_copy"]
    assert (work / "extra" / allowed).read_text() == "fixture"


def test_workspace_discovers_inputs_read_by_a_parent_conftest(tmp_path):
    from scripts.ci_mutation.runner import prepare_workspace

    root = tmp_path / "repo"
    (root / "tests/nested").mkdir(parents=True)
    (root / "records").mkdir()
    (root / "records/input.json").write_text("fixture")
    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (root / "tests/nested/test_selected.py").write_text("def test_selected():\n    pass\n")
    (root / "tests/conftest.py").write_text(
        "from pathlib import Path\nROOT = Path(__file__).resolve().parents[1]\n"
        '(ROOT / "records/input.json").read_text()\n'
    )
    work = tmp_path / "work"
    work.mkdir()
    prepare_workspace(root, work, ["hooks/sample.py"], ["tests/nested/test_selected.py::test_selected"])
    assert (work / "records/input.json").read_text() == "fixture"


def test_workspace_does_not_copy_inputs_referenced_through_repository_parent_traversal(tmp_path):
    from scripts.ci_mutation.runner import prepare_workspace

    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    (root / "tests/test_selected.py").write_text(
        'from pathlib import Path\nROOT = Path(__file__).resolve().parents[1]\n(ROOT / "../outside.txt").read_text()\n'
    )
    work = tmp_path / "nested/work"
    work.mkdir(parents=True)
    prepare_workspace(root, work, ["hooks/sample.py"], ["tests/test_selected.py"])
    assert outside.read_text() == "outside"
    assert not (work / "outside.txt").exists()
    assert not (work.parent / "outside.txt").exists()


@pytest.mark.parametrize("separator", ["\n", "\r"])
def test_workspace_preserves_valid_fixture_names_containing_line_breaks(tmp_path, separator):
    from scripts.ci_mutation.runner import prepare_workspace

    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "extra").mkdir()
    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    name = f"extra/line{separator}name.txt"
    (root / name).write_text("fixture")
    (root / "tests/test_selected.py").write_text(
        f"from pathlib import Path\nROOT = Path(__file__).resolve().parents[1]\n(ROOT / {name!r}).read_text()\n"
    )
    work = tmp_path / "work"
    work.mkdir()
    prepare_workspace(root, work, ["hooks/sample.py"], ["tests/test_selected.py"])
    assert (work / name).read_text() == "fixture"


def test_workspace_handles_a_selected_test_probing_an_invalid_null_path(tmp_path):
    from scripts.ci_mutation.runner import prepare_workspace

    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (root / "extra").mkdir()
    name = "extra/nul\0name"
    (root / "tests/test_selected.py").write_text(
        "import pytest\nfrom pathlib import Path\nROOT = Path(__file__).resolve().parents[1]\n"
        "with pytest.raises(ValueError):\n"
        f"    (ROOT / {name!r}).read_text()\n"
    )
    work = tmp_path / "work"
    work.mkdir()
    prepare_workspace(root, work, ["hooks/sample.py"], ["tests/test_selected.py"])
    assert not (work / "extra").exists()


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
    rows, error = mutate_files(tmp_path, tmp_path / "work", selected, 20, (1, 4), None)
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
        "1",
        "4",
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
            "1",
            "4",
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
    row = {
        "name": "hooks.sample.x_f__mutmut_1",
        "status": "survived",
        "lines": [2],
        "fingerprint": "abc",
        "diff": "-    return 1\n+    return 2",
    }

    def mutate(root, work, selected, deadline, shard, stats):
        assert shard == (2, 3)
        assert stats is None
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
    report = run_gate(tmp_path, {"hooks/sample.py": {changed}}, tmp_path / "output", 60, (2, 3))
    assert report["failed"] is fails
    assert report["files"][0]["counts"] == {"survived": 1}
    assert report["not_mutated"] == []
    assert json.loads((tmp_path / "output/report.json").read_text()) == report
    first, _, survivors = capsys.readouterr().out.partition("\n")
    assert json.loads(first) == report["files"][0]
    if fails:
        assert survivors.startswith("survived on lines 2: hooks/sample.py:hooks.sample.x_f__mutmut_1:abc\n")
        assert survivors.endswith("\n-    return 1\n+    return 2\n")
    else:
        assert survivors == ""


@pytest.mark.parametrize("partial", [False, True])
def test_shard_measurement_counts_owned_terminal_metadata_and_excludes_unchecked_rows(tmp_path, monkeypatch, partial):
    import io
    import itertools
    import json
    import sys

    from scripts.ci_mutation.stats import SharedStats

    (tmp_path / "hooks").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "hooks/sample.py").write_text("def f():\n    return 1\n")
    (tmp_path / "tests/test_sample.py").write_text("pass\n")
    stats = SharedStats(tmp_path / "stats", "head")
    stats.folder.mkdir()
    assignments = {"a": 1, "b": 1, "c": 1, "foreign": 0} if partial else {"a": 1, "b": 1}
    (stats.folder / "plan.json").write_text(
        json.dumps({"assignments": assignments, "costs": {name: 10 for name in assignments}})
    )

    def mutate(root, work, selected, deadline, shard, shared):
        if partial:
            for path in ("hooks/sample.py.meta", "hooks/duplicate.py.meta"):
                target = work / "mutants" / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps({"exit_code_by_key": {"a": 0, "b": None, "c": 1, "foreign": 0}}))
            return {}, "over budget"
        rows = [
            {"name": "a", "status": "killed"},
            {"name": "b", "status": "not checked", "lines": [], "fingerprint": "", "diff": ""},
        ]
        return {"hooks/sample.py": rows}, ""

    class Stream:
        def __init__(self):
            self.buffer = io.StringIO()
            self.flushed = ""

        def write(self, text):
            return self.buffer.write(text)

        def flush(self):
            self.flushed = self.buffer.getvalue()

    stream = Stream()
    ticks = itertools.chain([10], itertools.repeat(17))
    monkeypatch.setattr("scripts.ci_mutation.runner.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr("scripts.ci_mutation.runner.mutate_files", mutate)
    monkeypatch.setattr(sys, "stdout", stream)
    report = run_gate(tmp_path, {"hooks/sample.py": {2}}, tmp_path / "output", 1080, (1, 3), stats)
    assert report["shard"] == {
        "index": 1,
        "total": 3,
        "budget_seconds": 1080,
        "wall_seconds": 7,
        "expected": 3 if partial else 2,
        "graded": 2 if partial else 1,
        "complete": False,
        "budget_exhausted": partial,
        "estimated_worker_seconds": 30 if partial else 20,
    }
    assert json.loads(stream.buffer.getvalue().splitlines()[-1]) == {"shard": report["shard"]}
    assert stream.flushed == stream.buffer.getvalue()


def _stats_run(tmp_path, monkeypatch, statuses):
    commands = []

    def prepare(root, work, paths, tests):
        work.mkdir(exist_ok=True)

    def process(command, cwd, timeout, log):
        commands.append(command)
        if len(commands) == 2:
            (cwd / "results.json").write_text('{"hooks/sample.py": [{"status": "killed"}]}')
        return statuses[len(commands) - 1]

    monkeypatch.setattr("scripts.ci_mutation.runner.prepare_workspace", prepare)
    monkeypatch.setattr("scripts.ci_mutation.runner.run_process", process)
    monkeypatch.setattr("scripts.ci_mutation.runner.time.monotonic", lambda: 10)
    return commands


def test_a_stats_part_collects_once_and_skips_the_report(tmp_path, monkeypatch):
    from scripts.ci_mutation.runner import mutate_files
    from scripts.ci_mutation.stats import SharedStats, stats_key

    commands = _stats_run(tmp_path, monkeypatch, [0])
    selected = {"hooks/sample.py": ({2}, ["tests/test_sample.py"])}
    stats = SharedStats(tmp_path / "stats", "abc", (1, 3))
    rows, error = mutate_files(tmp_path, tmp_path / "work", selected, 20, (0, 1), stats)
    assert (rows, error) == ({"hooks/sample.py": []}, "")
    assert commands == [
        [
            sys.executable,
            "-m",
            "scripts.ci_mutation.selection",
            str(tmp_path / "work/changed-lines.json"),
            "0",
            "1",
            "collect",
            str(tmp_path / "stats/part-1.json"),
            stats_key("abc", selected),
            "1",
            "3",
        ]
    ]


def test_a_shard_mutates_with_every_matching_stats_part(tmp_path, monkeypatch):
    import json

    from scripts.ci_mutation.runner import mutate_files
    from scripts.ci_mutation.stats import SharedStats, stats_key, write_part

    commands = _stats_run(tmp_path, monkeypatch, [0, 0])
    selected = {"hooks/sample.py": ({2}, ["tests/test_sample.py"])}
    key = stats_key("abc", selected)
    write_part(tmp_path / "stats/part-0.json", key, (0, 2), [{"tests": {"f": ["t1"]}, "durations": {}, "cpu": 1}])
    write_part(tmp_path / "stats/part-1.json", key, (1, 2), [{"tests": {"g": ["t2"]}, "durations": {}, "cpu": 2}])
    stats = SharedStats(tmp_path / "stats", "abc")
    rows, error = mutate_files(tmp_path, tmp_path / "work", selected, 20, (2, 4), stats)
    assert (rows, error) == ({"hooks/sample.py": [{"status": "killed"}]}, "")
    assert commands[0][-4:] == ["2", "4", "reuse", str(tmp_path / "work/shared-stats.json")]
    assert json.loads((tmp_path / "work/shared-stats.json").read_text()) == [
        {"tests": {"f": ["t1"]}, "durations": {}, "cpu": 1},
        {"tests": {"g": ["t2"]}, "durations": {}, "cpu": 2},
    ]
    assert commands[1][3:6] == [str(tmp_path / "work/results.json"), "2", "4"]


@pytest.mark.parametrize("stale", ["", "head", "selection"])
def test_shards_reuse_exact_measured_plan_and_refuse_independently_stale_inputs(tmp_path, monkeypatch, stale):
    import json

    from scripts.ci_mutation.runner import mutate_files
    from scripts.ci_mutation.stats import SharedStats, stats_key, write_part

    commands = _stats_run(tmp_path, monkeypatch, [0, 0])
    selected = {"hooks/sample.py": ({2}, ["tests/test_sample.py"])}
    stats = SharedStats(tmp_path / "stats", "head")
    key = stats_key(stats.head, selected)
    write_part(stats.group(0).folder / "part-0.json", key, (0, 1), [])
    plan = {
        "head": "other" if stale == "head" else "head",
        "keys": ["other" if stale == "selection" else key],
        "shards": 4,
        "assignments": {"mutant": 2},
    }
    (stats.folder / "plan.json").write_text(json.dumps(plan))
    rows, reason = mutate_files(tmp_path, tmp_path / "work", selected, 20, (2, 4), stats.group(0))
    if stale:
        assert rows == {}
        assert reason == "measured mutation plan stale for this commit or selection"
        assert commands == []
    else:
        assert reason == ""
        assert rows == {"hooks/sample.py": [{"status": "killed"}]}
        assert json.loads((tmp_path / "work/shard-plan.json").read_text()) == plan
        assert commands[0][-4:] == ["2", "4", "reuse", str(tmp_path / "work/shared-stats.json")]


@pytest.mark.parametrize("kept", [True, False])
def test_shards_start_from_the_killing_tests_kept_beside_the_shared_stats(tmp_path, monkeypatch, kept):
    from scripts.ci_mutation.runner import mutate_files
    from scripts.ci_mutation.stats import SharedStats, stats_key, write_part

    _stats_run(tmp_path, monkeypatch, [0, 0])
    selected = {"hooks/sample.py": ({2}, ["tests/test_sample.py"])}
    stats = SharedStats(tmp_path / "stats", "head")
    write_part(stats.group(0).folder / "part-0.json", stats_key(stats.head, selected), (0, 1), [])
    if kept:
        (stats.folder / "killers").mkdir()
        (stats.folder / "killers/digest").write_text("tests/test_sample.py::t")
    rows, reason = mutate_files(tmp_path, tmp_path / "work", selected, 20, (0, 1), stats.group(0))
    assert reason == ""
    seeded = {path.name: path.read_text() for path in (tmp_path / "work").glob("killers/*")}
    assert seeded == ({"digest": "tests/test_sample.py::t"} if kept else {})


@pytest.mark.parametrize(
    ("head", "parts", "reason"),
    [
        ("abc", [], "mutation stats missing"),
        ("other", [0], "mutation stats stale"),
        ("abc", [1], "mutation stats missing"),
    ],
)
def test_a_shard_with_missing_or_stale_stats_fails_before_mutating(tmp_path, monkeypatch, capsys, head, parts, reason):
    from scripts.ci_mutation.stats import SharedStats, stats_key, write_part

    commands = _stats_run(tmp_path, monkeypatch, [0, 0])
    (tmp_path / "hooks").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "hooks/sample.py").write_text("def f():\n    return 1\n")
    (tmp_path / "tests/test_sample.py").write_text("from hooks.sample import f\n")
    key = stats_key(head, {"hooks/sample.py": ({2}, ["tests/test_sample.py"])})
    for part in parts:
        write_part(tmp_path / f"stats/group-0/part-{part}.json", key, (part, 2), [])
    stats = SharedStats(tmp_path / "stats", "abc")
    report = run_gate(tmp_path, {"hooks/sample.py": {2}}, tmp_path / "output", 60, (0, 1), stats)
    assert report["failed"]
    assert report["not_mutated"][0]["reason"].startswith(reason)
    assert commands == []
    assert f"hooks/sample.py: not mutated, {reason}" in capsys.readouterr().out


def test_workspace_links_tree_files_and_copies_when_links_fail(tmp_path, monkeypatch):
    from scripts.ci_mutation import runner

    source = tmp_path / "source.txt"
    source.write_text("kept\n")
    runner.link_or_copy(str(source), str(tmp_path / "linked.txt"))
    assert (tmp_path / "linked.txt").stat().st_ino == source.stat().st_ino

    def refuse(*args):
        raise OSError("cross device")

    monkeypatch.setattr(runner.os, "link", refuse)
    runner.link_or_copy(str(source), str(tmp_path / "copied.txt"))
    assert (tmp_path / "copied.txt").read_text() == "kept\n"
    assert (tmp_path / "copied.txt").stat().st_ino != source.stat().st_ino
    assert (tmp_path / "copied.txt").stat().st_mtime == source.stat().st_mtime
