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


def install_gate(root):
    package = root / "scripts" / "ci_prepush"
    package.mkdir(parents=True)
    (root / "scripts" / "__init__.py").write_text("")
    (package / "__init__.py").write_text("")
    (package / "__main__.py").write_text(
        "from pathlib import Path\nimport subprocess\n"
        "head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True)\n"
        "directory = subprocess.check_output(['git', 'rev-parse', '--absolute-git-dir'], text=True).strip()\n"
        "Path(directory, 'ci_prepush').write_text(head)\n"
    )
    git(root, "add", "scripts")
    git(root, "commit", "-qm", "Install repository gate")


def test_publish_selects_the_current_repository_and_retains_both_branches(repositories, monkeypatch):
    p = repositories
    p.task["repositories"] = [str(p.swarm), str(p.bundle)]
    fields = publish.prepare("swarm", p.agent, str(p.swarm), p.ledger)
    roots = [entry["worktree"] for entry in fields["worktrees"]]
    for root in roots:
        from pathlib import Path

        install_gate(Path(root))
        Path(root, "chosen.txt").write_text("after")
    monkeypatch.chdir(roots[1])
    first = publish.publish("swarm", p.agent, str(p.swarm), p.ledger, "Change bundle", ["chosen.txt"])
    assert first["worktree"] == roots[1]
    assert first["gate"] == str(p.bundle) + "-worktree/scripts/ci_prepush"
    assert git(roots[0], "show", "HEAD:chosen.txt") == "agentihooks"
    assert git(p.swarm, "ls-remote", "origin", "refs/heads/engineer-test") == ""
    monkeypatch.chdir(roots[0])
    second = publish.publish("swarm", p.agent, str(p.swarm), p.ledger, "Change hooks", ["chosen.txt"])
    assert second["worktree"] == roots[0]
    assert p.task["repository_branches"] == {str(p.bundle): "engineer-test", str(p.swarm): "engineer-test"}
    for root, repository in zip(roots, (p.swarm, p.bundle), strict=True):
        assert git(repository, "ls-remote", "origin", "refs/heads/engineer-test").split()[0] == git(
            root, "rev-parse", "HEAD"
        )
    assert publish.prepare("swarm", p.agent, str(p.swarm), p.ledger)["worktrees"] == fields["worktrees"]


@pytest.mark.parametrize(
    "source, succeeds",
    [
        ("value = 1\n", True),
        ("value =\n", False),
        ("import os\n", False),
        ("value=1\n", False),
        ("def chosen():\n" + "    print(1)\n" * 301, False),
        ("print(1)\n" * 3001, False),
        ("def chosen(a, b, c, d, e, f, g, h):\n    return a, b, c, d, e, f, g, h\n", False),
    ],
)
def test_bundle_without_a_gate_uses_the_named_touched_file_fallback(repositories, monkeypatch, source, succeeds):
    from pathlib import Path

    from scripts.swarm.store import SwarmError

    p = repositories
    (p.bundle / "untouched.py").write_text("broken =\n")
    git(p.bundle, "add", "untouched.py")
    git(p.bundle, "commit", "-qm", "Existing unrelated source")
    git(p.bundle, "push", "origin", "dev")
    p.task["repositories"] = [str(p.bundle)]
    root = Path(publish.prepare("swarm", p.agent, str(p.swarm), p.ledger)["worktree"])
    (root / "chosen.py").write_text(source)
    monkeypatch.chdir(root)
    if succeeds:
        result = publish.publish("swarm", p.agent, str(p.swarm), p.ledger, "Change bundle", ["chosen.py"])
        assert result["gate"] == "AgentiHooks touched Python fallback"
        assert result["worktree"] == str(root)
        assert git(p.bundle, "ls-remote", "origin", "refs/heads/engineer-test").split()[0] == result["head"]
    else:
        with pytest.raises(SwarmError, match="AgentiHooks touched Python fallback"):
            publish.publish("swarm", p.agent, str(p.swarm), p.ledger, "Bad bundle", ["chosen.py"])
        assert git(p.bundle, "ls-remote", "origin", "refs/heads/engineer-test") == ""
