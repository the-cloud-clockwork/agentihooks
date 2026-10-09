"""Tests for hooks.context.prepush_guard."""

import subprocess

import pytest

from hooks.context.prepush_guard import check_prepush
from hooks.hook_manager import BlockAction
from scripts import ci_prepush


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "scripts" / "ci_prepush").mkdir(parents=True)
    (root / "scripts" / "ci_prepush" / "__init__.py").write_text("")
    _git(root, "init", "-q", "-b", "work")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "scripts")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _bash(command, cwd):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}


def _stamp(root):
    ci_prepush.stamp_path(root).write_text(_git(root, "rev-parse", "HEAD") + "\n")


@pytest.mark.parametrize(
    "command",
    [
        "git push",
        "git push -u origin HEAD",
        "git push origin work:work",
        "echo hi && git push --set-upstream origin work",
    ],
)
def test_push_without_a_passing_run_is_blocked(repo, command):
    with pytest.raises(BlockAction, match="python -m scripts.ci_prepush"):
        check_prepush(_bash(command, repo))


def test_push_from_another_directory_resolves_the_repository(repo, tmp_path):
    with pytest.raises(BlockAction):
        check_prepush(_bash(f"cd {repo} && git push", tmp_path))
    with pytest.raises(BlockAction):
        check_prepush(_bash(f"git -C {repo} push origin HEAD", tmp_path))


def test_push_of_a_passing_head_is_allowed(repo):
    _stamp(repo)

    check_prepush(_bash("git push -u origin HEAD", repo))


def test_a_commit_after_the_run_blocks_again(repo):
    _stamp(repo)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "more")

    with pytest.raises(BlockAction):
        check_prepush(_bash("git push", repo))


@pytest.mark.parametrize(
    "command",
    [
        "git push origin --delete work",
        "git push origin -d work",
        "git push origin :work",
        "git push origin HEAD:diffcheck/plant",
        "git push --dry-run origin HEAD",
        "git status",
        "git commit -m 'then git push'",
        "echo git push",
    ],
)
def test_deletes_plants_dry_runs_and_other_commands_pass(repo, command):
    check_prepush(_bash(command, repo))


def test_a_repository_without_the_command_is_not_guarded(tmp_path):
    root = tmp_path / "other"
    root.mkdir()
    _git(root, "init", "-q")

    check_prepush(_bash("git push", root))
    check_prepush(_bash("git push", tmp_path))


def test_pre_tool_use_runs_the_guard_on_bash(repo, monkeypatch):
    from hooks import config, hook_manager

    monkeypatch.setattr(config, "SECRETS_MODE", "off")
    for flag in ("QUOTA_POLICY_ENABLED", "BROADCAST_ENABLED", "ENFORCEMENT_INJECTION_ENABLED", "RETRY_BREAKER_ENABLED"):
        monkeypatch.setattr(config, flag, False)

    with pytest.raises(BlockAction, match="python -m scripts.ci_prepush"):
        hook_manager.on_pre_tool_use({"session_id": "session", **_bash("git push -u origin HEAD", repo)})
