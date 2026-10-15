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
    (root / "sub").mkdir()
    (root / "sub" / "other.txt").write_text("unrelated nested file")
    git(
        root, "add", "scripts/__init__.py", "scripts/ci_prepush/__init__.py", "chosen.txt", "other.txt", "sub/other.txt"
    )
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


@pytest.fixture
def publication(repository, tmp_path, monkeypatch):
    worktree = tmp_path / "worktrees" / "repo" / "engineer-test"
    git(repository, "worktree", "add", "-b", "engineer-test", str(worktree))
    task = {"id": "t1", "claimed_by": "engineer@test", "state": "claimed", "branch": "engineer-test"}
    ledger = SimpleNamespace(
        tasks=lambda slug: [task], update_task=lambda slug, task_id, fields, by: task.update(fields)
    )
    agent = AgentRecord("engineer@test", "ci", "t1")
    gate = worktree / "scripts" / "ci_prepush" / "__main__.py"
    gate.write_text(
        "import os, subprocess, sys\n"
        "sys.exit(1) if os.environ.get('PUBLISH_GATE_FAIL') else None\n"
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
    return SimpleNamespace(root=worktree, task=task, agent=agent, ledger=ledger, repo=repository)


def test_dirty_push_names_selection_before_the_committed_head_gate(repository):
    (repository / "chosen.txt").write_text("after")
    with pytest.raises(BlockAction) as blocked:
        check_prepush({"tool_input": {"command": "git push"}, "cwd": str(repository)})
    assert f"Next: cd {repository} && git status --short" in str(blocked.value)


@pytest.mark.parametrize("deleted", ["other.txt", "sub/other.txt"])
def test_deleted_file_publication_retries_after_a_failed_gate(publication, monkeypatch, deleted):
    from scripts.swarm import publish
    from scripts.swarm.store import SwarmError

    root = publication.root
    (root / deleted).unlink()
    monkeypatch.setenv("PUBLISH_GATE_FAIL", "1")
    files = ["chosen.txt", deleted]
    with pytest.raises(SwarmError, match="Next: agentihooks swarm swarm publish"):
        publish.publish("swarm", publication.agent, str(publication.repo), publication.ledger, "Remove other", files)
    head = git(root, "rev-parse", "HEAD")
    assert deleted not in git(root, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    monkeypatch.delenv("PUBLISH_GATE_FAIL")
    publish.publish("swarm", publication.agent, str(publication.repo), publication.ledger, "Remove other", files)
    assert git(root, "rev-parse", "HEAD") == head
    assert git(root, "ls-remote", "origin", "refs/heads/engineer-test").split()[0] == head


def test_publication_commits_explicit_files_before_grading_and_pushing(publication):
    from scripts.swarm import publish

    worktree, task = publication.root, publication.task
    agent, ledger, repository = publication.agent, publication.ledger, publication.repo
    result = publish.publish("swarm", agent, str(repository), ledger, "Change chosen", ["chosen.txt"])
    head = git(worktree, "rev-parse", "HEAD")
    assert git(repository, "ls-remote", "origin", "refs/heads/engineer-test").split()[0] == head
    assert (worktree / ".git-gate-head").read_text().strip() == head
    assert git(worktree, "show", "HEAD:chosen.txt") == "after"
    assert result["branch"] == "engineer-test"
    assert task["branch"] == "engineer-test"


def test_prepare_creates_the_task_worktree_once_through_the_worktree_command(repository, tmp_path, monkeypatch):
    from scripts.swarm import publish, stack

    worktree = tmp_path / "worktrees" / "repo" / "engineer-test"
    script = tmp_path / "wt.sh"
    script.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        '[[ "$1" == new && "$2" == --repo ]]\n'
        'git -C "$3" worktree add -b engineer-test "$PUBLISH_PREPARED" >&2\n'
        'printf "%s\\n" "$PUBLISH_PREPARED"\n'
    )
    monkeypatch.setattr(stack, "WT_SCRIPT", script)
    monkeypatch.setenv("PUBLISH_PREPARED", str(worktree))
    task = {"id": "t1", "claimed_by": "engineer@test", "state": "claimed"}
    ledger = SimpleNamespace(tasks=lambda slug: [task])
    agent = AgentRecord("engineer@test", "ci", "t1")
    first = publish.prepare("swarm", agent, str(repository), ledger)
    assert first["worktree"] == str(worktree)
    assert first["branch"] == "engineer-test"
    assert publish.prepare("swarm", agent, str(repository), ledger) == first
    assert git(repository, "ls-remote", "origin", "refs/heads/engineer-test") == ""


def test_prepare_creates_a_continuation_from_the_recorded_remote_branch(repository, tmp_path, monkeypatch):
    from scripts.swarm import publish, stack

    git(repository, "push", "origin", "HEAD:predecessor")
    worktree = tmp_path / "worktrees" / "repo" / "engineer-test"
    script = tmp_path / "wt.sh"
    script.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        '[[ "$1" == new && "$2" == --repo && "$4" == --from && "$5" == origin/predecessor ]]\n'
        'git -C "$3" worktree add -b engineer-test "$PUBLISH_PREPARED" "$5" >&2\n'
        'printf "%s\\n" "$PUBLISH_PREPARED"\n'
    )
    monkeypatch.setattr(stack, "WT_SCRIPT", script)
    monkeypatch.setenv("PUBLISH_PREPARED", str(worktree))
    task = {"id": "t1", "claimed_by": "engineer@test", "state": "claimed", "branch": "predecessor"}
    ledger = SimpleNamespace(tasks=lambda slug: [task])
    agent = AgentRecord("engineer@test", "ci", "t1")
    result = publish.prepare("swarm", agent, str(repository), ledger)
    assert result["worktree"] == str(worktree)
    assert git(worktree, "rev-parse", "HEAD") == git(repository, "rev-parse", "origin/predecessor")


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


@pytest.mark.parametrize("staged", [False, True])
def test_unrelated_tracked_changes_are_preserved_and_refused(publication, staged):
    from scripts.swarm import publish
    from scripts.swarm.store import SwarmError

    root = publication.root
    (root / "other.txt").write_text("someone else's work")
    if staged:
        git(root, "add", "other.txt")
    before = git(root, "rev-parse", "HEAD")
    with pytest.raises(SwarmError, match="unrelated tracked changes remain: other.txt; Next: git -C"):
        publish.publish(
            "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
        )
    assert git(root, "rev-parse", "HEAD") == before
    assert (root / "other.txt").read_text() == "someone else's work"
    assert git(root, "diff", "--cached", "--name-only") == ("other.txt" if staged else "")
    assert git(root, "ls-remote", "origin", "refs/heads/engineer-test") == ""


def test_unrelated_untracked_files_are_never_staged(publication):
    from scripts.swarm import publish

    root = publication.root
    (root / "unknown.txt").write_text("someone else's work")
    publish.publish(
        "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
    )
    assert "unknown.txt" not in git(root, "ls-tree", "--name-only", "HEAD").splitlines()
    assert (root / "unknown.txt").read_text() == "someone else's work"


def test_failed_gate_never_pushes_and_can_retry_the_committed_head(publication, monkeypatch):
    from scripts.swarm import publish
    from scripts.swarm.store import SwarmError

    root = publication.root
    monkeypatch.setenv("PUBLISH_GATE_FAIL", "1")
    with pytest.raises(
        SwarmError, match="Next: agentihooks swarm swarm publish --message 'Change chosen' -- chosen.txt"
    ):
        publish.publish(
            "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
        )
    head = git(root, "rev-parse", "HEAD")
    assert git(root, "show", "HEAD:chosen.txt") == "after"
    assert git(root, "ls-remote", "origin", "refs/heads/engineer-test") == ""
    monkeypatch.delenv("PUBLISH_GATE_FAIL")
    publish.publish(
        "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
    )
    assert git(root, "rev-parse", "HEAD") == head
    assert git(root, "ls-remote", "origin", "refs/heads/engineer-test").split()[0] == head


@pytest.mark.parametrize("files", [["."], ["../chosen.txt"], ["/chosen.txt"], ["scripts"]])
def test_publication_rejects_broad_or_outside_file_selections(publication, files):
    from scripts.swarm import publish
    from scripts.swarm.store import SwarmError

    before = git(publication.root, "rev-parse", "HEAD")
    with pytest.raises(SwarmError, match="^publish accepts explicit repository relative file names only$"):
        publish.publish("swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", files)
    assert git(publication.root, "rev-parse", "HEAD") == before


def test_cli_prepares_and_publishes_from_the_primary_checkout(publication, monkeypatch, capsys):
    from scripts.swarm import cli

    agent, ledger = publication.agent, publication.ledger
    store = SimpleNamespace(
        names=SimpleNamespace(resolve=lambda name: name, swarm_slug=lambda slug: slug),
        agents=lambda slug: [agent],
        config=lambda slug: SimpleNamespace(repo=str(publication.repo)),
    )
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setattr(cli, "LedgerClient", lambda: ledger)
    assert cli.main(["swarm", "--as", agent.name, "prepare"]) == 0
    assert str(publication.root) in capsys.readouterr().out
    assert cli.main(["swarm", "--as", agent.name, "publish", "--message", "Change chosen", "--", "chosen.txt"]) == 0
    assert git(publication.repo, "ls-remote", "origin", "refs/heads/engineer-test").split()[0] == git(
        publication.root, "rev-parse", "HEAD"
    )


@pytest.mark.xdist_group("fakeredis")
def test_publication_preserves_the_enforced_build_gate(publication, monkeypatch):
    import fakeredis

    from scripts.swarm import publish, store
    from scripts.swarm.store import SwarmError

    fake = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(store, "redis_client", lambda environ: fake)
    agent = AgentRecord("engineer@a1b2c3-0001", "eng", "t1")
    publication.task["claimed_by"] = agent.name
    for key, value in {
        "AGENTIHOOKS_AGENT_NAME": agent.name,
        "AGENTIHOOKS_SWARM": "swarm",
        "AGENTIHOOKS_SWARM_TASK": "t1",
        "AGENTIHOOKS_GATE_BUILD": "enforce",
    }.items():
        monkeypatch.setenv(key, value)
    before = git(publication.root, "rev-parse", "HEAD")
    with pytest.raises(SwarmError):
        publish.publish("swarm", agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"])
    assert git(publication.root, "rev-parse", "HEAD") == before
    assert git(publication.root, "ls-remote", "origin", "refs/heads/engineer-test") == ""


@pytest.mark.parametrize("branch", ["dev", "main", "master", "v1"])
def test_protected_task_branches_never_prepare_or_publish(publication, branch):
    from scripts.swarm import publish
    from scripts.swarm.store import SwarmError

    publication.task["branch"] = branch
    before = git(publication.root, "rev-parse", "HEAD")
    with pytest.raises(SwarmError, match="outside the protected branches"):
        publish.prepare("swarm", publication.agent, str(publication.repo), publication.ledger)
    with pytest.raises(SwarmError, match="outside the protected branches"):
        publish.publish(
            "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
        )
    assert git(publication.root, "rev-parse", "HEAD") == before


def test_continuation_publishes_back_to_the_recorded_remote_branch(publication):
    from scripts.swarm import publish

    root = publication.root
    git(root, "push", "origin", "HEAD:predecessor")
    publication.task["branch"] = "predecessor"
    result = publish.publish(
        "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
    )
    head = git(root, "rev-parse", "HEAD")
    assert result["branch"] == publication.task["branch"] == "predecessor"
    assert git(root, "ls-remote", "origin", "refs/heads/predecessor").split()[0] == head
    assert git(root, "ls-remote", "origin", "refs/heads/engineer-test") == ""


@pytest.mark.parametrize("fields", [{"claimed_by": "another"}, {"state": "done"}])
def test_only_the_task_owner_can_publish(publication, fields):
    from scripts.swarm import publish
    from scripts.swarm.store import SwarmError

    publication.task.update(fields)
    with pytest.raises(SwarmError, match="publication requires this agent's claimed task"):
        publish.publish(
            "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
        )
    assert git(publication.root, "ls-remote", "origin", "refs/heads/engineer-test") == ""
