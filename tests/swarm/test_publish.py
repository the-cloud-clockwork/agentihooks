import json
import subprocess
from types import SimpleNamespace

import pytest

from hooks.context.prepush_guard import check_prepush
from hooks.hook_manager import BlockAction
from scripts.swarm.store import AgentRecord

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]


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
    updates = []

    def update(slug, task_id, fields, by):
        updates.append((slug, task_id, fields, by))
        task.update(fields)

    ledger = SimpleNamespace(tasks=lambda slug: {"swarm": [task]}[slug], update_task=update)
    agent = AgentRecord("engineer@test", "ci", "t1")
    gate = worktree / "scripts" / "ci_prepush" / "__main__.py"
    gate.write_text(
        "import os, subprocess, sys\n"
        "sys.exit('deliberate gate failure') if os.environ.get('PUBLISH_GATE_FAIL') else None\n"
        "from pathlib import Path\n"
        "assert subprocess.check_output(['git', 'show', 'HEAD:chosen.txt'], text=True) == 'after'\n"
        "assert not subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'], text=True)\n"
        "sys.exit(0) if os.environ.get('PUBLISH_GATE_UNSTAMPED') else None\n"
        "subprocess.run(['git', 'commit', '--allow-empty', '-qm', 'Different head'], check=True) if os.environ.get('PUBLISH_GATE_NEW_HEAD') else None\n"
        "head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True)\n"
        "Path('.git-gate-head').write_text(head)\n"
        "Path(subprocess.check_output(['git', 'rev-parse', '--absolute-git-dir'], text=True).strip(), 'ci_prepush').write_text(head)\n"
    )
    git(worktree, "add", "scripts/ci_prepush/__main__.py")
    git(worktree, "commit", "-qm", "Install gate fixture")
    (worktree / "chosen.txt").write_text("after")
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    return SimpleNamespace(root=worktree, task=task, agent=agent, ledger=ledger, repo=repository, updates=updates)


@pytest.mark.parametrize("failure", ["status", "spawn"])
def test_a_failed_dirty_lookup_keeps_the_unpassed_push_blocked(repository, monkeypatch, failure):
    from hooks.context import prepush_guard

    if failure == "status":
        git(repository, "config", "status.relativePaths", "invalid")
    else:
        native = subprocess.run

        def broken_status(argv, **kwargs):
            if "status" in argv:
                raise OSError("status process could not start")
            return native(argv, **kwargs)

        monkeypatch.setattr(prepush_guard.subprocess, "run", broken_status)
    with pytest.raises(BlockAction) as refused:
        check_prepush({"tool_input": {"command": "git push"}, "cwd": str(repository)})
    assert f"Next: cd {repository} && python -m scripts.ci_prepush" in str(refused.value)


def test_unrelated_untracked_files_do_not_change_the_gate_next_command(repository):
    (repository / "notes.txt").write_text("unrelated")
    with pytest.raises(BlockAction) as refused:
        check_prepush({"tool_input": {"command": "git push"}, "cwd": str(repository)})
    assert f"Next: cd {repository} && python -m scripts.ci_prepush" in str(refused.value)


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


@pytest.mark.parametrize("state", ["claimed", "pr"])
def test_publication_commits_explicit_files_before_grading_and_pushing(publication, state):
    from scripts.swarm import publish

    worktree, task = publication.root, publication.task
    agent, ledger, repository = publication.agent, publication.ledger, publication.repo
    task["state"] = state
    result = publish.publish("swarm", agent, str(repository), ledger, "Change chosen", ["chosen.txt"])
    head = git(worktree, "rev-parse", "HEAD")
    assert git(repository, "ls-remote", "origin", "refs/heads/engineer-test").split()[0] == head
    assert (worktree / ".git-gate-head").read_text().strip() == head
    assert git(worktree, "show", "HEAD:chosen.txt") == "after"
    assert result["branch"] == "engineer-test"
    assert task["branch"] == "engineer-test"
    fields = {"branch": "engineer-test", "branch_repo": str(repository.parent / "remote.git")}
    assert result == {
        "worktree": str(worktree),
        **fields,
        "head": head,
        "gate": str(worktree / "scripts" / "ci_prepush"),
    }
    assert publication.updates == [
        ("swarm", "t1", {**fields, "repository_branches": {str(repository): "engineer-test"}}, agent.name)
    ]
    assert git(worktree, "rev-parse", "--abbrev-ref", "@{upstream}") == "origin/engineer-test"


@pytest.mark.parametrize("record_repository_branch", [False, True])
def test_publish_from_second_worktree_pushes_and_records_its_branch(
    publication, tmp_path, monkeypatch, record_repository_branch
):
    from scripts.swarm import publish

    first = publication.root
    git(first, "push", "origin", "HEAD:engineer-test")
    first_head = git(first, "rev-parse", "HEAD")
    second = tmp_path / "second"
    git(first, "worktree", "add", "-b", "engineer-test-2", str(second))
    if record_repository_branch:
        publication.task["repository_branches"] = {str(publication.repo): "engineer-test"}
    (second / "chosen.txt").write_text("after")
    monkeypatch.chdir(second)

    result = publish.publish(
        "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
    )

    head = git(second, "rev-parse", "HEAD")
    assert result["worktree"] == str(second)
    assert result["branch"] == publication.task["branch"] == "engineer-test-2"
    assert publication.task["repository_branches"] == {str(publication.repo): "engineer-test-2"}
    assert git(second, "ls-remote", "origin", "refs/heads/engineer-test-2").split()[0] == head
    assert git(first, "ls-remote", "origin", "refs/heads/engineer-test").split()[0] == first_head
    assert git(second, "rev-parse", "--abbrev-ref", "@{upstream}") == "origin/engineer-test-2"


def test_prepare_creates_the_task_worktree_once_through_the_worktree_command(repository, tmp_path, monkeypatch):
    from scripts.swarm import publish, stack

    worktree = tmp_path / "worktrees" / "repo" / "engineer-test"
    script = tmp_path / "wt.sh"
    script.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        '[[ "$1" == new && "$2" == --repo && "$PWD" == "$3" ]]\n'
        'git -C "$3" worktree add -b engineer-test "$PUBLISH_PREPARED" >&2\n'
        'printf "%s\\n" "$PUBLISH_PREPARED"\n'
    )
    monkeypatch.setattr(stack, "WT_SCRIPT", script)
    monkeypatch.setenv("PUBLISH_PREPARED", str(worktree))
    task = {"id": "t1", "claimed_by": "engineer@test", "state": "claimed"}
    ledger = SimpleNamespace(
        tasks=lambda slug: {"swarm": [task]}[slug], update_task=lambda slug, task_id, fields, by: task.update(fields)
    )
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
        '[[ "$1" == new && "$2" == --repo && "$4" == --from && "$5" == origin/predecessor && "$PWD" == "$3" ]]\n'
        'git -C "$3" worktree add -b engineer-test "$PUBLISH_PREPARED" "$5" >&2\n'
        'printf "%s\\n" "$PUBLISH_PREPARED"\n'
    )
    monkeypatch.setattr(stack, "WT_SCRIPT", script)
    monkeypatch.setenv("PUBLISH_PREPARED", str(worktree))
    task = {"id": "t1", "claimed_by": "engineer@test", "state": "claimed", "branch": "predecessor"}
    ledger = SimpleNamespace(
        tasks=lambda slug: {"swarm": [task]}[slug], update_task=lambda slug, task_id, fields, by: task.update(fields)
    )
    agent = AgentRecord("engineer@test", "ci", "t1")
    result = publish.prepare("swarm", agent, str(repository), ledger)
    assert result["worktree"] == str(worktree)
    assert git(worktree, "rev-parse", "HEAD") == git(repository, "rev-parse", "origin/predecessor")
    assert task["branch"] == result["branch"] == "engineer-test"
    assert task["branch_repo"] == str(repository.parent / "remote.git")
    assert task["repository_branches"] == {str(repository): "engineer-test"}


def test_prepare_reuses_the_recorded_worktree_without_pushing(repository, tmp_path):
    from scripts.swarm import publish

    worktree = tmp_path / "worktrees" / "repo" / "engineer-test"
    git(repository, "worktree", "add", "-b", "engineer-test", str(worktree))
    task = {"id": "t1", "claimed_by": "engineer@test", "state": "claimed", "branch": "engineer-test"}
    ledger = SimpleNamespace(
        tasks=lambda slug: {"swarm": [task]}[slug], update_task=lambda slug, task_id, fields, by: task.update(fields)
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
    ledger = SimpleNamespace(
        tasks=lambda slug: {"swarm": [task]}[slug], update_task=lambda slug, task_id, fields, by: task.update(fields)
    )
    agent = AgentRecord("engineer@test", "ci", "t1")
    with pytest.raises(SwarmError, match="Next: agentihooks swarm swarm prepare"):
        publish.publish("swarm", agent, str(repository), ledger, "Change chosen", ["chosen.txt"])


def test_publish_requires_an_explicit_commit_message():
    from scripts.swarm import cli

    with pytest.raises(SystemExit) as refused:
        cli.main(["swarm", "publish", "--", "chosen.txt"])
    assert refused.value.code == 2


@pytest.mark.parametrize("gate_env", ["PUBLISH_GATE_UNSTAMPED", "PUBLISH_GATE_NEW_HEAD"])
def test_only_a_pass_for_the_original_committed_head_can_push(publication, monkeypatch, gate_env):
    from scripts.swarm import publish
    from scripts.swarm.store import SwarmError

    monkeypatch.setenv(gate_env, "1")
    with pytest.raises(SwarmError) as refused:
        publish.publish(
            "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
        )
    assert (
        str(refused.value)
        == "the committed HEAD has no passing prepush evidence; Next: agentihooks swarm swarm publish --message 'Change chosen' -- chosen.txt"
    )
    assert git(publication.root, "ls-remote", "origin", "refs/heads/engineer-test") == ""


def test_a_missing_claim_has_a_named_refusal(publication):
    from scripts.swarm import publish
    from scripts.swarm.store import SwarmError

    publication.ledger.tasks = lambda slug: []
    with pytest.raises(SwarmError, match="^publication requires this agent's claimed task$"):
        publish.publish(
            "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
        )


def test_a_successor_uses_the_recorded_worktree_among_other_checkouts(publication, tmp_path):
    from scripts.swarm import publish

    root = publication.root
    git(root, "worktree", "add", "-b", "unrelated", str(tmp_path / "unrelated"))
    agent = AgentRecord("engineer@successor", "ci", "t1")
    publication.task["claimed_by"] = agent.name
    result = publish.publish("swarm", agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"])
    assert result["worktree"] == str(root)
    assert result["head"] == git(root, "rev-parse", "HEAD")


@pytest.mark.parametrize("branch", [None, "", "XXXX"])
def test_a_detached_checkout_never_becomes_the_unrecorded_task_worktree(publication, tmp_path, capfd, branch):
    from scripts.swarm import publish

    root = publication.root
    git(root, "worktree", "add", "--detach", str(tmp_path / "detached"))
    if branch is None:
        publication.task.pop("branch")
    else:
        publication.task["branch"] = branch
    result = publish.publish(
        "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
    )
    assert result["worktree"] == str(root)
    assert result["branch"] == publication.task["branch"] == "engineer-test"
    assert "fatal" not in capfd.readouterr().err


def test_multiple_unrecorded_task_worktrees_refuse_an_ambiguous_publish(publication, tmp_path):
    from scripts.swarm import publish
    from scripts.swarm.store import SwarmError

    git(publication.root, "worktree", "add", "-b", "engineer-test-2", str(tmp_path / "engineer-test-2"))
    publication.task.pop("branch")
    with pytest.raises(SwarmError, match="^multiple task worktrees match; record the task branch before publishing$"):
        publish.publish(
            "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
        )


@pytest.mark.parametrize("selected", ["*.txt", "--literal.txt"])
def test_file_names_are_staged_literally(publication, selected):
    from scripts.swarm import publish

    root = publication.root
    (root / selected).write_text("literal name")
    (root / "unknown.txt").write_text("unrelated")
    publish.publish(
        "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt", selected]
    )
    names = git(root, "ls-tree", "--name-only", "HEAD").splitlines()
    assert selected in names
    assert "unknown.txt" not in names


def test_display_filters_cannot_hide_a_staged_change(publication):
    from scripts.swarm import publish

    root = publication.root
    (root / ".gitattributes").write_text("chosen.txt diff=constant\n")
    git(root, "config", "diff.constant.textconv", "printf constant")
    git(root, "add", ".gitattributes")
    git(root, "commit", "-qm", "Configure display filter")
    result = publish.publish(
        "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
    )
    assert git(root, "show", "HEAD:chosen.txt") == "after"
    assert result["head"] == git(root, "rev-parse", "HEAD")


@pytest.mark.parametrize("name", [" ", " leading.txt"])
def test_a_whitespace_file_name_does_not_hide_the_need_to_commit(publication, name):
    from scripts.swarm import publish

    root = publication.root
    (root / name).write_text("before")
    git(root, "add", "chosen.txt", name)
    git(root, "commit", "-qm", "Seed whitespace name")
    (root / name).write_text("after")
    publish.publish(
        "swarm", publication.agent, str(publication.repo), publication.ledger, "Change whitespace file", [name]
    )
    assert git(root, "show", f"HEAD:{name}") == "after"


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


@pytest.mark.parametrize("staged", [False, True])
def test_unrelated_refusal_names_every_file_and_the_exact_worktree(publication, staged):
    from scripts.swarm import publish
    from scripts.swarm.store import SwarmError

    root = publication.root
    (root / "other.txt").write_text("someone else's work")
    (root / "sub" / "other.txt").write_text("someone else's nested work")
    if staged:
        git(root, "add", "other.txt", "sub/other.txt")
    with pytest.raises(SwarmError) as refused:
        publish.publish(
            "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
        )
    assert (
        str(refused.value)
        == f"unrelated tracked changes remain: other.txt, sub/other.txt; Next: git -C {root} status --short"
    )


@pytest.mark.parametrize(
    "missing, expected",
    [
        (
            ["missing.txt"],
            "selected files do not exist: missing.txt; Next: agentihooks swarm swarm publish --message 'Change chosen' -- chosen.txt missing.txt",
        ),
        (
            ["missing.txt", "also-missing.txt"],
            "selected files do not exist: also-missing.txt, missing.txt; Next: agentihooks swarm swarm publish --message 'Change chosen' -- chosen.txt missing.txt also-missing.txt",
        ),
    ],
)
def test_missing_selected_files_are_refused_before_staging(publication, missing, expected):
    from scripts.swarm import publish
    from scripts.swarm.store import SwarmError

    before = git(publication.root, "rev-parse", "HEAD")
    with pytest.raises(SwarmError) as refused:
        publish.publish(
            "swarm",
            publication.agent,
            str(publication.repo),
            publication.ledger,
            "Change chosen",
            ["chosen.txt", *missing],
        )
    assert str(refused.value) == expected
    assert git(publication.root, "rev-parse", "HEAD") == before
    assert git(publication.root, "diff", "--cached", "--name-only") == ""


@pytest.mark.parametrize("deleted", ["other.txt", "new-other.txt"])
def test_an_already_staged_deletion_is_published(publication, deleted):
    from scripts.swarm import publish

    root = publication.root
    if deleted == "new-other.txt":
        (root / deleted).write_text("new file")
        git(root, "add", deleted)
        git(root, "commit", "-qm", "Seed new file")
    git(root, "rm", deleted)
    result = publish.publish(
        "swarm",
        publication.agent,
        str(publication.repo),
        publication.ledger,
        "Delete other",
        ["chosen.txt", deleted],
    )
    assert deleted not in git(root, "ls-tree", "--name-only", "HEAD").splitlines()
    assert result["head"] == git(root, "rev-parse", "HEAD")


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
    ) as refused:
        publish.publish(
            "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
        )
    assert "deliberate gate failure" in str(refused.value)
    head = git(root, "rev-parse", "HEAD")
    assert git(root, "show", "HEAD:chosen.txt") == "after"
    assert git(root, "ls-remote", "origin", "refs/heads/engineer-test") == ""
    monkeypatch.delenv("PUBLISH_GATE_FAIL")
    publish.publish(
        "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
    )
    assert git(root, "rev-parse", "HEAD") == head
    assert git(root, "ls-remote", "origin", "refs/heads/engineer-test").split()[0] == head


def fake_python(path, log, ruff):
    import sys

    path.parent.mkdir(parents=True, exist_ok=True)
    run = f'exec {sys.executable} "$@"' if ruff else "echo 'No module named ruff' >&2\nexit 1"
    path.write_text(f'#!/bin/sh\necho "$*" >> {log}\n{run}\n')
    path.chmod(0o755)
    return str(path)


@pytest.mark.parametrize("tool_has_ruff", [False, True])
def test_the_gate_runs_with_the_workspace_venv_python(publication, monkeypatch, tmp_path, tool_has_ruff):
    from scripts.swarm import publish

    venv_log, tool_log = tmp_path / "venv.log", tmp_path / "tool.log"
    fake_python(publication.repo.parent / ".venv" / "bin" / "python", venv_log, True)
    monkeypatch.setattr(publish.sys, "executable", fake_python(tmp_path / "tool" / "python", tool_log, tool_has_ruff))
    publish.publish(
        "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
    )
    head = git(publication.root, "rev-parse", "HEAD")
    assert git(publication.repo, "ls-remote", "origin", "refs/heads/engineer-test").split()[0] == head
    assert venv_log.read_text().splitlines() == ["-I -c import ruff", "-m scripts.ci_prepush"]
    assert not tool_log.exists()
    assert publish.gate_python(publication.repo) == str(publication.repo.parent / ".venv" / "bin" / "python")


def test_a_workspace_venv_without_ruff_falls_back_to_the_running_python(publication, monkeypatch, tmp_path, capfd):
    from scripts.swarm import publish

    venv_log, tool_log = tmp_path / "venv.log", tmp_path / "tool.log"
    fake_python(publication.repo.parent / ".venv" / "bin" / "python", venv_log, False)
    monkeypatch.setattr(publish.sys, "executable", fake_python(tmp_path / "tool" / "python", tool_log, True))
    capfd.readouterr()
    publish.publish(
        "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
    )
    head = git(publication.root, "rev-parse", "HEAD")
    assert git(publication.repo, "ls-remote", "origin", "refs/heads/engineer-test").split()[0] == head
    assert venv_log.read_text().splitlines() == ["-I -c import ruff"]
    assert tool_log.read_text().splitlines() == ["-I -c import ruff", "-m scripts.ci_prepush"]
    assert capfd.readouterr() == ("", "")


def test_no_python_with_ruff_refuses_before_the_push(publication, monkeypatch, tmp_path):
    import re

    from scripts.swarm import publish
    from scripts.swarm.store import SwarmError

    venv = publication.repo.parent / ".venv" / "bin" / "python"
    fake_python(venv, tmp_path / "venv.log", False)
    monkeypatch.setattr(
        publish.sys, "executable", fake_python(tmp_path / "tool" / "python", tmp_path / "tool.log", False)
    )
    expected = (
        f"no python with ruff for the prepush gate; install the dev extra into {venv}; "
        "Next: agentihooks swarm swarm publish --message 'Change chosen' -- chosen.txt"
    )
    with pytest.raises(SwarmError, match=f"^{re.escape(expected)}$"):
        publish.publish(
            "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
        )
    assert git(publication.repo, "ls-remote", "origin", "refs/heads/engineer-test") == ""


@pytest.mark.parametrize("files", [["."], ["../chosen.txt"], ["/chosen.txt"], ["scripts"]])
def test_publication_rejects_broad_or_outside_file_selections(publication, files):
    from scripts.swarm import publish
    from scripts.swarm.store import SwarmError

    before = git(publication.root, "rev-parse", "HEAD")
    with pytest.raises(SwarmError, match="^publish accepts explicit repository relative file names only$"):
        publish.publish("swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", files)
    assert git(publication.root, "rev-parse", "HEAD") == before


@pytest.mark.parametrize("option", ["--message", "-m"])
def test_cli_prepares_and_publishes_from_the_primary_checkout(publication, monkeypatch, capsys, option):
    from scripts.swarm import cli

    agent, ledger = publication.agent, publication.ledger
    store = SimpleNamespace(
        names=SimpleNamespace(resolve=lambda name: name, swarm_slug=lambda slug: slug),
        agents=lambda slug: {"swarm": [agent]}[slug],
        config=lambda slug: {"swarm": SimpleNamespace(repo=str(publication.repo))}[slug],
    )
    monkeypatch.chdir(publication.repo)
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setattr(cli, "LedgerClient", lambda: ledger)
    assert cli.main(["swarm", "--as", agent.name, "prepare"]) == 0
    prepared = json.loads(capsys.readouterr().out)
    checkout = {
        "worktree": str(publication.root),
        "branch": "engineer-test",
        "branch_repo": str(publication.repo.parent / "remote.git"),
    }
    assert prepared == {**checkout, "repository": str(publication.repo), "repository_source": "swarm default"}
    assert cli.main(["swarm", "--as", agent.name, "publish", option, "Change chosen", "--", "chosen.txt"]) == 0
    head = git(publication.root, "rev-parse", "HEAD")
    assert json.loads(capsys.readouterr().out) == {
        **checkout,
        "head": head,
        "gate": str(publication.root / "scripts" / "ci_prepush"),
    }
    assert git(publication.repo, "ls-remote", "origin", "refs/heads/engineer-test").split()[0] == head


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
    with pytest.raises(SwarmError, match="trace-plan"):
        publish.publish("swarm", agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"])
    assert git(publication.root, "rev-parse", "HEAD") == before
    assert git(publication.root, "ls-remote", "origin", "refs/heads/engineer-test") == ""


@pytest.mark.parametrize("branch", ["dev", "main", "master", "v1"])
def test_protected_task_branches_never_prepare_or_publish(publication, branch):
    from scripts.swarm import publish
    from scripts.swarm.store import SwarmError

    publication.task["branch"] = branch
    before = git(publication.root, "rev-parse", "HEAD")
    with pytest.raises(SwarmError, match="^publication requires a task branch outside the protected branches$"):
        publish.prepare("swarm", publication.agent, str(publication.repo), publication.ledger)
    with pytest.raises(SwarmError, match="^publication requires a task branch outside the protected branches$"):
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
    assert git(root, "rev-parse", "--abbrev-ref", "@{upstream}") == "origin/predecessor"


def test_a_restacked_successor_publishes_its_prepared_branch_not_the_parked_one(publication):
    from scripts.swarm import publish

    root, repository = publication.root, publication.repo
    parked = git(repository, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "Parked predecessor work")
    git(repository, "push", "origin", f"{parked}:refs/heads/predecessor")
    publication.task["branch"] = "predecessor"
    result = publish.publish(
        "swarm", publication.agent, str(repository), publication.ledger, "Change chosen", ["chosen.txt"]
    )
    head = git(root, "rev-parse", "HEAD")
    assert result["branch"] == publication.task["branch"] == "engineer-test"
    assert git(root, "ls-remote", "origin", "refs/heads/engineer-test").split()[0] == head
    assert git(root, "ls-remote", "origin", "refs/heads/predecessor").split()[0] == parked
    fields = {
        "branch": "engineer-test",
        "branch_repo": str(repository.parent / "remote.git"),
        "repository_branches": {str(repository): "engineer-test"},
    }
    assert publication.updates == [("swarm", "t1", fields, publication.agent.name)]


@pytest.mark.parametrize("prepared", [False, True])
def test_parked_successor_restacks_and_publishes_with_predecessor_checkout_present(
    publication, tmp_path, monkeypatch, prepared
):
    from scripts.swarm import publish, stack

    p = publication
    root, repository = p.root, p.repo
    (root / "chosen.txt").write_text("before")
    git(repository, "switch", "-c", "blocker")
    (repository / "blocker.txt").write_text("dependency")
    git(repository, "add", "blocker.txt")
    git(repository, "commit", "-qm", "Dependency work")
    base = git(repository, "rev-parse", "HEAD")
    git(root, "merge", "--no-edit", "blocker")
    (root / "task.txt").write_text("parked work")
    git(root, "add", "task.txt")
    git(root, "commit", "-qm", "Parked task work")
    parked = git(root, "rev-parse", "HEAD")
    git(root, "push", "origin", "HEAD:predecessor")
    git(repository, "switch", "dev")
    git(repository, "merge", "--squash", "blocker")
    git(repository, "commit", "-qm", "Squash dependency")
    git(repository, "push", "origin", "dev")
    git(root, "branch", "XX--verifyXX")
    p.task.update(branch="predecessor", parked_on=["dependency"], stacked_base=base)
    p.ledger.state = lambda slug: {"tasks": [p.task, {"id": "dependency", "state": "done"}]}
    if prepared:
        result = publish.prepare("swarm", p.agent, str(repository), p.ledger)
        assert result["worktree"] == str(root)
        assert p.task["branch"] == "engineer-test"
    predecessor = tmp_path / "predecessor"
    git(repository, "worktree", "add", "-b", "predecessor", str(predecessor), "origin/predecessor")
    monkeypatch.chdir(root)

    stack.restack("swarm", p.agent, p.ledger)

    assert p.task["branch"] == "engineer-test"
    assert p.task["repository_branches"] == {str(repository): "engineer-test"}
    assert p.task["parked_on"] == []
    (root / "chosen.txt").write_text("after")
    result = publish.publish("swarm", p.agent, str(repository), p.ledger, "Continue task", ["chosen.txt"])

    head = git(root, "rev-parse", "HEAD")
    assert result["worktree"] == str(root)
    assert result["branch"] == p.task["branch"] == "engineer-test"
    assert git(root, "ls-remote", "origin", "refs/heads/engineer-test").split()[0] == head
    assert git(root, "ls-remote", "origin", "refs/heads/predecessor").split()[0] == parked
    assert git(predecessor, "rev-parse", "HEAD") == parked
    assert (root / "task.txt").read_text() == "parked work"


@pytest.mark.parametrize("fields", [{"claimed_by": "another"}, {"state": "done"}])
def test_only_the_task_owner_can_publish(publication, fields):
    from scripts.swarm import publish
    from scripts.swarm.store import SwarmError

    publication.task.update(fields)
    with pytest.raises(SwarmError, match="^publication requires this agent's claimed task$"):
        publish.publish(
            "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
        )
    assert git(publication.root, "ls-remote", "origin", "refs/heads/engineer-test") == ""
