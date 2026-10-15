import subprocess
from types import SimpleNamespace

import pytest

from hooks.context.prepush_guard import check_prepush
from hooks.hook_manager import BlockAction
from scripts.swarm.store import AgentRecord

pytestmark = pytest.mark.unit


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "dev")
    git(root, "config", "user.name", "test")
    git(root, "config", "user.email", "test@example.com")
    gate = root / "scripts" / "ci_prepush"
    gate.mkdir(parents=True)
    (root / "scripts" / "__init__.py").write_text("")
    (gate / "__init__.py").write_text("")
    (root / "chosen.txt").write_text("before")
    (root / "other.txt").write_text("unrelated")
    git(root, "add", "scripts/__init__.py", "scripts/ci_prepush/__init__.py", "chosen.txt", "other.txt")
    git(root, "commit", "-qm", "base")
    remote = tmp_path / "remote.git"
    git(root, "init", "--bare", str(remote))
    git(root, "remote", "add", "origin", str(remote))
    git(root, "push", "-u", "origin", "dev")
    return root


def test_out_of_order_push_names_the_committed_head_gate(repository):
    with pytest.raises(BlockAction) as blocked:
        check_prepush({"tool_input": {"command": "git push"}, "cwd": str(repository)})
    assert f"Next: cd {repository} && python -m scripts.ci_prepush" in str(blocked.value)


def test_publication_commits_explicit_files_before_grading_and_pushing(repository, tmp_path, monkeypatch):
    from scripts.swarm import publish

    worktree = tmp_path / "worktrees" / "repo" / "engineer-test"
    git(repository, "worktree", "add", "-b", "engineer-test", str(worktree))
    task = {"id": "t1", "claimed_by": "engineer@test", "state": "claimed", "branch": "engineer-test"}
    ledger = SimpleNamespace(
        tasks=lambda slug: [task], update_task=lambda slug, task_id, fields, by: task.update(fields)
    )
    agent = AgentRecord("engineer@test", "ci", "t1")
    gate = worktree / "scripts" / "ci_prepush" / "__main__.py"
    gate.write_text(
        "import subprocess\n"
        "from pathlib import Path\n"
        "assert subprocess.check_output(['git', 'show', 'HEAD:chosen.txt'], text=True) == 'after'\n"
        "assert not subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'], text=True)\n"
        "head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True)\n"
        "Path('.git-gate-head').write_text(head)\n"
        "Path(subprocess.check_output(['git', 'rev-parse', '--absolute-git-dir'], text=True).strip(), 'ci_prepush').write_text(head)\n"
    )
    git(worktree, "add", "scripts/ci_prepush/__main__.py")
    git(worktree, "commit", "-qm", "Install gate fixture")
    (worktree / "chosen.txt").write_text("after")
    monkeypatch.chdir(repository)
    result = publish.publish("swarm", agent, str(repository), ledger, "Change chosen", ["chosen.txt"])
    head = git(worktree, "rev-parse", "HEAD")
    assert git(repository, "ls-remote", "origin", "refs/heads/engineer-test").split()[0] == head
    assert (worktree / ".git-gate-head").read_text().strip() == head
    assert git(worktree, "show", "HEAD:chosen.txt") == "after"
    assert result["branch"] == "engineer-test"
    assert task["branch"] == "engineer-test"


def test_prepare_reuses_the_recorded_worktree_without_pushing(repository, tmp_path):
    from scripts.swarm import publish

    worktree = tmp_path / "worktrees" / "repo" / "engineer-test"
    git(repository, "worktree", "add", "-b", "engineer-test", str(worktree))
    task = {"id": "t1", "claimed_by": "engineer@test", "state": "claimed", "branch": "engineer-test"}
    ledger = SimpleNamespace(
        tasks=lambda slug: [task], update_task=lambda slug, task_id, fields, by: task.update(fields)
    )
    agent = AgentRecord("engineer@test", "ci", "t1")
    first = publish.prepare("swarm", agent, str(repository), ledger)
    assert first["worktree"] == str(worktree)
    assert publish.prepare("swarm", agent, str(repository), ledger) == first
    assert git(repository, "ls-remote", "origin", "refs/heads/engineer-test") == ""


def test_publication_without_a_prepared_worktree_names_the_prepare_command(repository):
    from scripts.swarm import publish
    from scripts.swarm.store import SwarmError

    task = {"id": "t1", "claimed_by": "engineer@test", "state": "claimed"}
    ledger = SimpleNamespace(tasks=lambda slug: [task])
    agent = AgentRecord("engineer@test", "ci", "t1")
    with pytest.raises(SwarmError, match="Next: agentihooks swarm swarm prepare"):
        publish.publish("swarm", agent, str(repository), ledger, "Change chosen", ["chosen.txt"])
