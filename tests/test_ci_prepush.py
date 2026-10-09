import subprocess
import sys

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
            "scripts/size_limits.py": "GRADER = 1\n",
            "tests/test_alpha.py": "from scripts.alpha import A\n",
            "tests/test_beta.py": "from scripts.beta import B\n",
            "tests/sub/helpers.py": "H = 1\n",
            "tests/sub/test_gamma.py": "G = 1\n",
            "tests/sub/test_delta.py": "D = 1\n",
            "tests/sub/deep/test_epsilon.py": "E = 1\n",
            "tests/helpers.py": "R = 1\n",
            "tests/SIZE_ALLOWLIST.json": '{"base": {}}\n',
        },
        "base",
    )
    _git(root, "branch", "base")
    return root


class _Runner:
    def __init__(self, failing=()):
        self.failing, self.calls = set(failing), []

    def __call__(self, command, cwd, env):
        self.calls.append((command, cwd, env))
        return subprocess.CompletedProcess(command, 1 if command[2] in self.failing else 0)


def test_tests_for_selects_changed_tests_and_importers_grouped_by_directory(repo):
    _commit(repo, {"tests/sub/test_gamma.py": "G = 2\n", "scripts/alpha.py": "A = 2\n", "README.md": "x\n"}, "change")

    assert ci_prepush.tests_for(repo, ci_prepush.changed(repo, "base")) == [
        "tests/test_alpha.py",
        "tests/sub/test_gamma.py",
    ]


def test_a_changed_test_helper_selects_the_tests_beside_and_below_it(repo):
    _commit(repo, {"tests/sub/helpers.py": "H = 2\n"}, "helper")

    assert ci_prepush.tests_for(repo, ci_prepush.changed(repo, "base")) == [
        "tests/sub/test_delta.py",
        "tests/sub/test_gamma.py",
        "tests/sub/deep/test_epsilon.py",
    ]


def test_a_root_test_helper_selects_only_the_root_tests(repo):
    _commit(repo, {"tests/helpers.py": "R = 2\n"}, "root helper")

    assert ci_prepush.tests_for(repo, ci_prepush.changed(repo, "base")) == [
        "tests/test_alpha.py",
        "tests/test_beta.py",
    ]


def test_a_test_named_file_outside_tests_is_not_run_itself(repo):
    _commit(repo, {"scripts/test_tool.py": "T = 1\n"}, "tool")

    assert ci_prepush.tests_for(repo, ci_prepush.changed(repo, "base")) == []


def test_tests_for_skips_deleted_files(repo):
    _git(repo, "rm", "-q", "tests/test_beta.py")
    _git(repo, "commit", "-q", "-m", "drop")

    assert ci_prepush.changed(repo, "base") == []


def test_an_unknown_base_fails_loudly(repo):
    with pytest.raises(subprocess.CalledProcessError):
        ci_prepush.changed(repo, "no-such-base")


def test_plan_grades_size_with_the_base_grader_and_runs_two_workers(repo, tmp_path):
    _commit(repo, {"scripts/beta.py": "B = 2\n", "scripts/size_limits.py": "GRADER = 2\n"}, "change")
    grade = tmp_path / "grade"
    (grade / "tests").mkdir(parents=True)

    steps = dict(ci_prepush.plan(repo, "base", grade))

    assert steps == {
        "ruff check": [sys.executable, "-m", "ruff", "check", "hooks/", "scripts/", "tests/"],
        "ruff format": [sys.executable, "-m", "ruff", "format", "--check", "hooks/", "scripts/", "tests/"],
        "size limits": [
            sys.executable,
            "-I",
            str(grade / "scripts/size_limits.py"),
            "--base",
            str(grade),
            "--head",
            str(repo),
        ],
        "tests": [sys.executable, "-m", "pytest", "-n", "2", "--dist", "loadgroup", "-q", "tests/test_beta.py"],
    }
    assert (grade / "tests/SIZE_ALLOWLIST.json").read_text() == '{"base": {}}\n'
    assert (grade / "scripts/size_limits.py").read_text() == "GRADER = 1\n"


def test_plan_skips_tests_when_the_change_touches_none(repo, tmp_path):
    _commit(repo, {"README.md": "x\n"}, "docs")

    assert [name for name, _ in ci_prepush.plan(repo, "base", tmp_path / "grade")] == [
        "ruff check",
        "ruff format",
        "size limits",
    ]


def test_run_stamps_head_when_every_step_passes(repo, monkeypatch, capsys):
    _commit(repo, {"scripts/alpha.py": "A = 3\n"}, "change")
    (repo / "notes.txt").write_text("untracked\n")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_REDIS_URL", "redis://x")
    monkeypatch.setenv("REDIS_PASSWORD", "x")
    runner = _Runner()

    assert ci_prepush.run(repo, "base", runner) == 0

    assert ci_prepush.passed(repo)
    head = _git(repo, "rev-parse", "HEAD")
    assert capsys.readouterr().out == (
        "ci_prepush: ruff check\nci_prepush: ruff format\nci_prepush: size limits\nci_prepush: tests\n"
        f"ci_prepush: every cheap gate passed on {head[:12]}; push it.\n"
    )
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
    assert capsys.readouterr().out.endswith(
        "ci_prepush: ruff check, ruff format failed; fix them, commit and run again before pushing.\n"
    )


def test_a_failed_rerun_voids_an_earlier_pass_on_the_same_head(repo):
    assert ci_prepush.run(repo, "base", _Runner()) == 0

    assert ci_prepush.run(repo, "base", _Runner(failing={"pytest", "ruff"})) == 1

    assert not ci_prepush.passed(repo)


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
    assert (
        capsys.readouterr().out == "ci_prepush: commit or set aside the tracked changes first; the gates grade HEAD.\n"
    )


def test_the_default_base_is_fetched_from_origin_dev(repo, tmp_path):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "dev")
    _git(repo, "fetch", "-q", "origin")
    _git(repo, "checkout", "-q", "-b", "work")
    _commit(repo, {"scripts/beta.py": "B = 3\n"}, "change")
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", "-b", "dev", str(origin), str(other))
    _git(other, "config", "user.email", "t@example.com")
    _git(other, "config", "user.name", "t")
    _commit(other, {"README.md": "moved\n"}, "dev moves")
    _git(other, "tag", "v9")
    _git(other, "push", "-q", "origin", "dev", "HEAD:refs/heads/side", "v9")
    runner = _Runner()

    assert ci_prepush.run(repo, ci_prepush.BASE, runner) == 0

    assert runner.calls[-1][0][-1] == "tests/test_beta.py"
    assert _git(repo, "rev-parse", "origin/dev") == _git(other, "rev-parse", "HEAD")
    assert _git(repo, "branch", "-r") == "origin/dev"
    assert _git(repo, "tag") == ""


def test_the_stamp_lives_in_the_git_dir(repo):
    assert ci_prepush.stamp_path(repo) == repo.resolve() / ".git" / "ci_prepush"


def test_passed_is_false_outside_a_repository(tmp_path):
    assert not ci_prepush.passed(tmp_path)


@pytest.mark.parametrize(("argv", "base"), [([], "origin/dev"), (["--base", "base"], "base")])
def test_main_runs_in_the_repository_top_level(repo, monkeypatch, argv, base):
    seen = []
    monkeypatch.chdir(repo / "tests")
    monkeypatch.setattr(ci_prepush, "run", lambda root, base, *_: seen.append((root, base)) or 0)

    assert ci_prepush.main(argv) == 0

    assert seen == [(repo.resolve(), base)]
