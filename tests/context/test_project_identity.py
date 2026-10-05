import subprocess
from pathlib import Path

import pytest

from hooks.context.project_identity import resolve_project


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def test_primary_subfolder_and_worktree_share_identity(monkeypatch, tmp_path):
    repo = tmp_path / "alpha"
    repo.mkdir()
    git(repo, "init")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "--allow-empty", "-m", "init")
    git(repo, "remote", "add", "origin", "https://github.com/example/alpha.git")
    sub = repo / "sub"
    sub.mkdir()
    worktree = tmp_path / "work"
    git(repo, "worktree", "add", "-b", "proof", str(worktree))
    primary = resolve_project(str(repo), {})
    assert primary.project == "alpha"
    assert primary.repo == "alpha"
    assert primary.remote == "example/alpha"
    assert resolve_project(str(sub), {}).repo == primary.repo
    linked = resolve_project(str(worktree), {})
    assert linked.repo == primary.repo
    assert linked.project == primary.project
    assert linked.worktree == "work"
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", tmp_path / "state")
    config = tmp_path / "state" / "swarm" / "test" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text('{"repo": "' + str(repo) + '"}')
    assert resolve_project(str(worktree), {"AGENTIHOOKS_SWARM": "test"}).worktree == "work"


@pytest.mark.parametrize("cwd", ["", "/nonexistent"])
def test_unowned_folder_has_no_project(cwd):
    assert resolve_project(cwd, {}) is None


def test_home_and_scratch_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert resolve_project(str(tmp_path), {}) is None
    identity = resolve_project(str(tmp_path / "scratchpad" / "alpha" / "task"), {})
    assert identity.project == "alpha"
    assert identity.repo == "alpha"


def test_swarm_repo_overrides_working_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", tmp_path / ".agentihooks")
    config = tmp_path / ".agentihooks" / "swarm" / "proof" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text('{"repo": "' + str(tmp_path / "scratchpad" / "alpha" / "task") + '"}')
    assert resolve_project(str(tmp_path), {"AGENTIHOOKS_SWARM": "proof"}).project == "alpha"
