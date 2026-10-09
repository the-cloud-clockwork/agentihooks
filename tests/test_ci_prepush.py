import subprocess
import sys
from pathlib import Path

import pytest

from scripts import ci_prepush


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout.strip()


def _commit(root, files, message):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(root, "add", *files)
    _git(root, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "dev")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _commit(
        root,
        {
            "scripts/alpha.py": "A = 1\n",
            "scripts/beta.py": "B = 1\n",
            "tests/test_alpha.py": "from scripts.alpha import A\n",
            "tests/test_beta.py": "from scripts.beta import B\n",
            "tests/sub/test_gamma.py": "G = 1\n",
            "tests/SIZE_ALLOWLIST.json": '{"base": {}}\n',
        },
        "base",
    )
    _git(root, "branch", "base")
    return root


def test_tests_for_selects_changed_tests_and_importers_grouped_by_directory(repo):
    _commit(repo, {"tests/sub/test_gamma.py": "G = 2\n", "scripts/alpha.py": "A = 2\n", "README.md": "x\n"}, "change")

    assert ci_prepush.tests_for(repo, ci_prepush.changed(repo, "base")) == [
        "tests/test_alpha.py",
        "tests/sub/test_gamma.py",
    ]


def test_tests_for_skips_deleted_files(repo):
    _git(repo, "rm", "-q", "tests/test_beta.py")
    _git(repo, "commit", "-q", "-m", "drop")

    assert ci_prepush.changed(repo, "base") == []


def test_plan_runs_lint_format_size_against_the_base_allowlist_and_two_workers(repo, tmp_path):
    _commit(repo, {"scripts/beta.py": "B = 2\n"}, "change")

    steps = dict(ci_prepush.plan(repo, "base", tmp_path / "grade"))

    assert steps["ruff check"][1:] == ["-m", "ruff", "check", "hooks/", "scripts/", "tests/"]
    assert steps["ruff format"][1:] == ["-m", "ruff", "format", "--check", "hooks/", "scripts/", "tests/"]
    assert steps["size limits"][1:] == [
        "-m",
        "scripts.size_limits",
        "--base",
        str(tmp_path / "grade"),
        "--head",
        str(repo),
    ]
    assert (tmp_path / "grade" / "tests" / "SIZE_ALLOWLIST.json").read_text() == '{"base": {}}\n'
    assert steps["tests"][1:5] == ["-m", "pytest", "-n", "2"]
    assert steps["tests"][-1:] == ["tests/test_beta.py"]
    assert {command[0] for command in steps.values()} == {sys.executable}


def test_plan_skips_tests_when_the_change_touches_none(repo, tmp_path):
    _commit(repo, {"README.md": "x\n"}, "docs")

    assert [name for name, _ in ci_prepush.plan(repo, "base", tmp_path / "grade")] == [
        "ruff check",
        "ruff format",
        "size limits",
    ]


class _Runner:
    def __init__(self, failing=()):
        self.failing, self.calls = set(failing), []

    def __call__(self, command, cwd, env):
        self.calls.append((command, cwd, env))
        return subprocess.CompletedProcess(command, 1 if command[2] in self.failing else 0)


def test_run_stamps_head_when_every_step_passes(repo, monkeypatch):
    _commit(repo, {"scripts/alpha.py": "A = 3\n"}, "change")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_REDIS_URL", "redis://x")
    monkeypatch.setenv("REDIS_PASSWORD", "x")
    runner = _Runner()

    assert ci_prepush.run(repo, "base", runner) == 0

    assert ci_prepush.passed(repo)
    assert len(runner.calls) == 4
    for _, cwd, env in runner.calls:
        assert cwd == repo
        assert not [name for name in env if "REDIS" in name]
        assert env["PATH"]


def test_run_keeps_going_after_a_failure_and_leaves_no_stamp(repo, capsys):
    _commit(repo, {"scripts/alpha.py": "A = 3\n"}, "change")
    runner = _Runner(failing={"ruff"})

    assert ci_prepush.run(repo, "base", runner) == 1

    assert not ci_prepush.passed(repo)
    assert len(runner.calls) == 4
    assert "ruff check, ruff format failed" in capsys.readouterr().out


def test_a_new_commit_voids_the_stamp(repo):
    assert ci_prepush.run(repo, "base", _Runner()) == 0
    _commit(repo, {"scripts/alpha.py": "A = 4\n"}, "next")

    assert not ci_prepush.passed(repo)


def test_run_refuses_uncommitted_tracked_changes(repo, capsys):
    (repo / "scripts/alpha.py").write_text("A = 5\n")
    runner = _Runner()

    assert ci_prepush.run(repo, "base", runner) == 1

    assert runner.calls == []
    assert not ci_prepush.passed(repo)
    assert "commit" in capsys.readouterr().out


def test_passed_is_false_outside_a_repository(tmp_path):
    assert not ci_prepush.passed(tmp_path)


def test_main_runs_in_the_repository_top_level(repo, monkeypatch):
    seen = []
    monkeypatch.chdir(repo / "tests")
    monkeypatch.setattr(ci_prepush, "run", lambda root, base, *_: seen.append((root, base)) or 0)

    assert ci_prepush.main(["--base", "base"]) == 0

    assert seen == [(Path(repo).resolve(), "base")]
