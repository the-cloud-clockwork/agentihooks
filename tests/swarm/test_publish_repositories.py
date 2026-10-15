import subprocess
from types import SimpleNamespace

import pytest

from scripts.swarm import publish, stack
from scripts.swarm.store import AgentRecord

pytestmark = pytest.mark.unit


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def repositories(tmp_path, monkeypatch):
    roots = []
    for name in ("agentihooks", "agentihooks-bundle"):
        root = tmp_path / name
        root.mkdir()
        git(root, "init", "-q", "-b", "dev")
        git(root, "config", "user.name", "test")
        git(root, "config", "user.email", "test@example.com")
        (root / "chosen.txt").write_text(name)
        git(root, "add", "chosen.txt")
        git(root, "commit", "-qm", "base")
        remote = tmp_path / f"{name}.git"
        git(root, "init", "--bare", str(remote))
        git(root, "remote", "add", "origin", str(remote))
        git(root, "push", "-u", "origin", "dev")
        roots.append(root)
    script = tmp_path / "wt.sh"
    script.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        '[[ "$1" == new && "$2" == --repo && "$PWD" == "$3" ]]\n'
        'git -C "$3" fetch origin dev >&2\n'
        'dest="$3-worktree"\n'
        'git -C "$3" worktree add -b engineer-test "$dest" origin/dev >&2\n'
        'printf "%s\\n" "$dest"\n'
    )
    monkeypatch.setattr(stack, "WT_SCRIPT", script)
    task = {"id": "t1", "claimed_by": "engineer@test", "state": "claimed"}
    updates = []

    def update(slug, task_id, fields, by):
        assert (slug, task_id, by) == ("swarm", "t1", "engineer@test")
        updates.append(fields)
        task.update(fields)

    return SimpleNamespace(
        swarm=roots[0],
        bundle=roots[1],
        task=task,
        agent=AgentRecord("engineer@test", "eng", "t1"),
        ledger=SimpleNamespace(tasks=lambda slug: [task], update_task=update),
        updates=updates,
    )


def test_prepare_uses_the_explicit_bundle_repository(repositories):
    p = repositories
    p.task["repositories"] = [str(p.bundle)]
    fields = publish.prepare("swarm", p.agent, str(p.swarm), p.ledger)
    assert fields["worktree"] == f"{p.bundle}-worktree"
    assert git(p.bundle, "rev-parse", "origin/dev") == git(fields["worktree"], "rev-parse", "HEAD")
    assert git(fields["worktree"], "show", "HEAD:chosen.txt") == "agentihooks-bundle"
    assert publish.prepare("swarm", p.agent, str(p.swarm), p.ledger) == fields
    assert len(git(p.swarm, "worktree", "list", "--porcelain").split("worktree ")) == 2


def test_prepare_keeps_one_worktree_per_repository(repositories):
    p = repositories
    p.task["repositories"] = [str(p.swarm), str(p.bundle)]
    fields = publish.prepare("swarm", p.agent, str(p.swarm), p.ledger)
    roots = [entry["worktree"] for entry in fields["worktrees"]]
    assert roots == [f"{p.swarm}-worktree", f"{p.bundle}-worktree"]
    assert [git(root, "show", "HEAD:chosen.txt") for root in roots] == ["agentihooks", "agentihooks-bundle"]
    assert publish.prepare("swarm", p.agent, str(p.swarm), p.ledger) == fields


def test_prepare_names_the_default_repository_without_inferring_prose(repositories):
    p = repositories
    p.task["description"] = f"Work in {p.bundle}"
    fields = publish.prepare("swarm", p.agent, str(p.swarm), p.ledger)
    assert fields["worktree"] == f"{p.swarm}-worktree"
    assert fields["repository_source"] == "swarm default"
    assert fields["repository"] == str(p.swarm)
