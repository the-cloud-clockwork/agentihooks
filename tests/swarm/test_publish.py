import json
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
    publication.task["branch"] = "engineer-test-2"
    if record_repository_branch:
        publication.task["repository_branches"] = {str(publication.repo): "engineer-test-2"}
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


@pytest.mark.parametrize("repository_branch", [False, True])
def test_publish_registered_worktree_ignores_a_prerequisite_pull_request(publication, monkeypatch, repository_branch):
    from scripts.swarm import publish

    root = publication.root
    git(root, "push", "origin", "HEAD:prerequisite")
    original = git(root, "rev-parse", "HEAD")
    publication.task["pr_url"] = "https://github.com/example/repo/pull/7"
    if repository_branch:
        publication.task["branch"] = "prerequisite"
        publication.task["repository_branches"] = {str(publication.repo): "engineer-test"}
    monkeypatch.chdir(root)
    monkeypatch.setattr(publish.publish_queue, "open_head", lambda remote, url: "prerequisite")

    result = publish.publish(
        "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
    )

    head = git(root, "rev-parse", "HEAD")
    assert result["worktree"] == str(root)
    assert result["branch"] == "engineer-test"
    assert git(root, "ls-remote", "origin", "refs/heads/engineer-test").split()[0] == head
    assert git(root, "ls-remote", "origin", "refs/heads/prerequisite").split()[0] == original


@pytest.mark.parametrize("repository_branch", [False, True])
@pytest.mark.parametrize("cwd", ["worktree", "primary"])
def test_publish_refuses_a_registered_branch_mismatch_before_any_mutation(
    publication, monkeypatch, repository_branch, cwd
):
    from scripts.swarm import publish
    from scripts.swarm.store import SwarmError

    root = publication.root
    git(root, "push", "origin", "HEAD:prerequisite")
    original = git(root, "rev-parse", "HEAD")
    publication.task["branch"] = "prerequisite"
    if repository_branch:
        publication.task["branch"] = "engineer-test"
        publication.task["repository_branches"] = {str(publication.repo): "prerequisite"}
    monkeypatch.chdir(root if cwd == "worktree" else publication.repo)

    with pytest.raises(SwarmError, match="engineer-test.*registered.*prerequisite"):
        publish.publish(
            "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
        )

    assert git(root, "rev-parse", "HEAD") == original
    assert git(root, "diff", "--cached") == ""
    assert (root / "chosen.txt").read_text() == "after"
    assert publication.updates == []
    assert git(root, "ls-remote", "origin", "refs/heads/prerequisite").split()[0] == original
    assert git(root, "ls-remote", "origin", "refs/heads/engineer-test") == ""


@pytest.mark.parametrize("binding", ["recorded", "tracked", "continuation"])
def test_stacked_publication_records_its_current_branch_before_pushing(publication, monkeypatch, binding):
    from scripts.swarm import publish

    root = publication.root
    git(root, "push", "origin", "HEAD:prerequisite")
    original = git(root, "rev-parse", "HEAD")
    publication.task.update(branch="engineer-test", pr_url="https://github.com/example/repo/pull/7")
    if binding == "tracked":
        git(root, "branch", "--set-upstream-to=origin/prerequisite")
    if binding == "continuation":
        publication.task["handoff_envelope"] = {"continue_from": "origin/prerequisite"}
    monkeypatch.chdir(root)
    native = publish.execute
    pushes = []

    def observe(argv, where):
        if argv[:2] == ["git", "push"]:
            assert publication.task["branch"] == "engineer-test"
            assert publication.task["repository_branches"] == {str(publication.repo): "engineer-test"}
            pushes.append(argv)
        return native(argv, where)

    monkeypatch.setattr(publish, "execute", observe)
    result = publish.publish(
        "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
    )

    head = git(root, "rev-parse", "HEAD")
    assert result["branch"] == publication.task["branch"] == "engineer-test"
    assert len(pushes) == 1
    assert git(root, "ls-remote", "origin", "refs/heads/engineer-test").split()[0] == head
    assert git(root, "ls-remote", "origin", "refs/heads/prerequisite").split()[0] == original


@pytest.mark.parametrize("queued", [False, True])
def test_publication_checks_the_exact_target_branch_before_pushing(publication, monkeypatch, queued):
    from scripts.swarm import merge_queue, publish
    from scripts.swarm.store import SwarmError

    root = publication.root
    native = publish.git
    pushes = []
    queries = []

    def git_call(where, *args):
        if args == ("remote", "get-url", "origin"):
            return "https://github.com/example/fetch-repo.git"
        if args == ("remote", "get-url", "--push", "origin"):
            return "git@github.com:example/repo.git"
        if args[:1] == ("push",):
            pushes.append(args)
        return native(where, *args)

    def query(document, variables):
        assert "mergeQueueEntry" in document
        assert "headRefName" in document
        assert variables == {"owner": "example", "name": "repo", "branch": "engineer-test"}
        queries.append(variables)
        return {
            "repository": {
                "pullRequests": {
                    "nodes": [
                        {
                            "url": "https://github.com/example/repo/pull/8",
                            "mergeQueueEntry": {"id": "entry"} if queued else None,
                        }
                    ],
                    "pageInfo": {"hasNextPage": False},
                }
            }
        }

    monkeypatch.setattr(publish, "git", git_call)
    monkeypatch.setattr(merge_queue, "graphql", query)
    if queued:
        with pytest.raises(SwarmError, match="merge queue"):
            publish.publish(
                "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
            )
        assert pushes == []
        assert git(root, "ls-remote", "origin", "refs/heads/engineer-test") == ""
    else:
        result = publish.publish(
            "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
        )
        assert result["branch"] == "engineer-test"
        assert len(pushes) == 1
    assert len(queries) == 1


def test_prepared_continuation_publishes_its_current_branch(publication, tmp_path, monkeypatch):
    from scripts.swarm import publish

    first = publication.root
    git(first, "push", "origin", "HEAD:engineer-test")
    second = tmp_path / "second"
    git(first, "worktree", "add", "-b", "engineer-test-2", str(second))
    publication.task.update(branch="engineer-test-2", handoff_envelope={"continue_from": "origin/engineer-test"})
    (second / "chosen.txt").write_text("after")
    monkeypatch.chdir(second)

    result = publish.publish(
        "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
    )

    head = git(second, "rev-parse", "HEAD")
    assert result["branch"] == publication.task["branch"] == "engineer-test-2"
    assert git(second, "ls-remote", "origin", "refs/heads/engineer-test-2").split()[0] == head
    assert git(second, "ls-remote", "origin", "refs/heads/engineer-test").split()[0] != head
    assert git(second, "rev-parse", "--abbrev-ref", "@{upstream}") == "origin/engineer-test-2"


def test_publish_never_continues_a_protected_branch(publication, monkeypatch):
    from scripts.swarm import publish

    dev = git(publication.root, "ls-remote", "origin", "refs/heads/dev").split()[0]
    publication.task["handoff_envelope"] = {"continue_from": "origin/dev"}
    (publication.root / "chosen.txt").write_text("after")
    monkeypatch.chdir(publication.root)

    result = publish.publish(
        "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
    )

    assert result["branch"] == "engineer-test"
    assert git(publication.root, "ls-remote", "origin", "refs/heads/dev").split()[0] == dev


def test_publish_does_not_recreate_a_deleted_retained_branch(publication, tmp_path, monkeypatch):
    from scripts.swarm import publish

    first = publication.root
    git(first, "push", "origin", "HEAD:engineer-test")
    origin = git(first, "remote", "get-url", "--push", "origin")
    git(first, "push", origin, "--delete", "engineer-test")
    second = tmp_path / "second"
    git(first, "worktree", "add", "-b", "engineer-test-2", str(second))
    publication.task.update(branch="engineer-test-2", handoff_envelope={"continue_from": "origin/engineer-test"})
    (second / "chosen.txt").write_text("after")
    monkeypatch.chdir(second)

    result = publish.publish(
        "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
    )
    assert result["branch"] == publication.task["branch"] == "engineer-test-2"
    assert git(second, "ls-remote", "origin", "refs/heads/engineer-test-2").split()[0] == result["head"]

    assert git(second, "ls-remote", "origin", "refs/heads/engineer-test") == ""


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


def test_prepare_stacks_on_the_recorded_branch_and_publish_pushes_its_current_branch(repository, tmp_path, monkeypatch):
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
    assert git(worktree, "rev-parse", "--abbrev-ref", "@{upstream}") == "origin/predecessor"

    (worktree / "chosen.txt").write_text("after")
    gate = worktree / "scripts" / "ci_prepush" / "__main__.py"
    gate.write_text(
        "import subprocess\nfrom pathlib import Path\n"
        "head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True)\n"
        "Path(subprocess.check_output(['git', 'rev-parse', '--absolute-git-dir'], text=True).strip(), 'ci_prepush').write_text(head)\n"
    )
    monkeypatch.chdir(worktree)
    published = publish.publish(
        "swarm", agent, str(repository), ledger, "Continue", ["chosen.txt", str(gate.relative_to(worktree))]
    )

    head = git(worktree, "rev-parse", "HEAD")
    assert published["branch"] == task["branch"] == "engineer-test"
    assert task["repository_branches"] == {str(repository): "engineer-test"}
    assert git(repository, "ls-remote", "origin", "refs/heads/engineer-test").split()[0] == head
    assert git(repository, "ls-remote", "origin", "refs/heads/predecessor").split()[0] != head


def test_publish_uses_its_current_branch_over_inherited_tracking(publication):
    from scripts.swarm import publish

    root = publication.root
    git(root, "push", "origin", "HEAD:predecessor")
    git(root, "fetch", "origin")
    git(root, "branch", "--set-upstream-to=origin/predecessor")
    publication.task.update(branch="predecessor", repository_branches={str(publication.repo): "engineer-test"})

    result = publish.publish(
        "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
    )

    head = git(root, "rev-parse", "HEAD")
    assert result["branch"] == publication.task["branch"] == "engineer-test"
    assert publication.task["repository_branches"] == {str(publication.repo): "engineer-test"}
    assert git(root, "ls-remote", "origin", "refs/heads/engineer-test").split()[0] == head
    assert git(root, "ls-remote", "origin", "refs/heads/predecessor").split()[0] != head


def test_publish_never_follows_a_tracked_protected_branch(publication):
    from scripts.swarm import publish

    root = publication.root
    dev = git(root, "ls-remote", "origin", "refs/heads/dev").split()[0]
    git(root, "fetch", "origin")
    git(root, "branch", "--set-upstream-to=origin/dev")
    publication.task["repository_branches"] = {str(publication.repo): "engineer-test", "/elsewhere/bundle": "dev"}

    result = publish.publish(
        "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
    )

    assert result["branch"] == "engineer-test"
    assert git(root, "ls-remote", "origin", "refs/heads/dev").split()[0] == dev
    assert git(root, "ls-remote", "origin", "refs/heads/engineer-test").split()[0] == git(root, "rev-parse", "HEAD")


def test_publish_never_follows_a_tracked_branch_the_task_does_not_record(publication):
    from scripts.swarm import publish

    root = publication.root
    git(root, "push", "origin", "HEAD:another-task")
    other = git(root, "rev-parse", "HEAD")
    git(root, "branch", "--set-upstream-to=origin/another-task")

    result = publish.publish(
        "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
    )

    assert result["branch"] == publication.task["branch"] == "engineer-test"
    assert git(root, "ls-remote", "origin", "refs/heads/another-task").split()[0] == other
    assert git(root, "ls-remote", "origin", "refs/heads/engineer-test").split()[0] == git(root, "rev-parse", "HEAD")


def test_tracked_returns_an_upstream_recorded_only_for_its_repository(publication):
    from scripts.swarm import publish

    root = publication.root
    git(root, "push", "origin", "HEAD:predecessor")
    git(root, "branch", "--set-upstream-to=origin/predecessor")
    scoped = {"branch": "engineer-test", "repository_branches": {str(publication.repo): "predecessor"}}

    assert publish.tracked(root, scoped) == "predecessor"
    assert publish.tracked(root, {"branch": "engineer-test", "repository_branches": {}}) == ""
    assert publish.tracked(root, {"branch": "engineer-test"}) == ""


def test_prepare_tracks_the_recorded_delivery_branch_from_an_existing_worktree(repository, tmp_path):
    from scripts.swarm import publish

    git(repository, "push", "origin", "HEAD:predecessor")
    worktree = tmp_path / "worktrees" / "repo" / "engineer-test"
    git(repository, "worktree", "add", "-b", "engineer-test", str(worktree))
    task = {"id": "t1", "claimed_by": "engineer@test", "state": "claimed", "branch": "predecessor"}
    ledger = SimpleNamespace(
        tasks=lambda slug: {"swarm": [task]}[slug], update_task=lambda slug, task_id, fields, by: task.update(fields)
    )

    result = publish.prepare("swarm", AgentRecord("engineer@test", "ci", "t1"), str(repository), ledger)

    assert result["worktree"] == str(worktree)
    assert task["branch"] == result["branch"] == "engineer-test"
    assert git(worktree, "rev-parse", "--abbrev-ref", "@{upstream}") == "origin/predecessor"


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
    from scripts.swarm.store import SwarmError

    root = publication.root
    git(root, "worktree", "add", "--detach", str(tmp_path / "detached"))
    if branch is None:
        publication.task.pop("branch")
    else:
        publication.task["branch"] = branch
    if branch == "XXXX":
        with pytest.raises(SwarmError, match="engineer-test.*registered.*XXXX"):
            publish.publish(
                "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
            )
        assert publication.updates == []
    else:
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


@pytest.fixture
def conflicted_publication(publication):
    root = publication.root
    (root / "chosen.txt").write_text("task edit")
    git(root, "add", "chosen.txt")
    git(root, "commit", "-qm", "Task edit")
    (publication.repo / "chosen.txt").write_text("dev edit")
    (publication.repo / "other.txt").write_text("inherited dev edit")
    (publication.repo / "sub" / "other.txt").unlink()
    (publication.repo / "inherited.txt").write_text("inherited addition")
    git(publication.repo, "add", "chosen.txt", "other.txt", "sub/other.txt", "inherited.txt")
    git(publication.repo, "commit", "-qm", "Dev edits")
    git(publication.repo, "push", "origin", "dev")
    result = subprocess.run(["git", "merge", "--no-edit", "origin/dev"], cwd=root, capture_output=True, text=True)
    assert result.returncode == 1
    assert git(root, "diff", "--name-only", "--diff-filter=U") == "chosen.txt"
    (root / "chosen.txt").write_text("after")
    git(root, "add", "chosen.txt")
    return publication


def test_publish_excludes_files_inherited_from_a_conflicted_dev_merge(conflicted_publication):
    from scripts.swarm import publish

    p = conflicted_publication
    result = publish.publish("swarm", p.agent, str(p.repo), p.ledger, "Merge dev", ["chosen.txt"])
    assert git(p.root, "rev-list", "--parents", "-n", "1", result["head"]).count(" ") == 2
    assert git(p.root, "show", "HEAD:other.txt") == "inherited dev edit"
    assert git(p.root, "show", "HEAD:inherited.txt") == "inherited addition"
    assert git(p.root, "ls-tree", "--name-only", "HEAD", "sub/other.txt") == ""
    assert git(p.root, "rev-parse", "origin/engineer-test") == result["head"]


@pytest.mark.parametrize("staged", [False, True])
def test_publish_still_refuses_unrelated_edits_during_a_dev_merge(conflicted_publication, staged):
    from scripts.swarm import publish
    from scripts.swarm.store import SwarmError

    p = conflicted_publication
    (p.root / "other.txt").write_text("unrelated edit")
    if staged:
        git(p.root, "add", "other.txt")
    before = git(p.root, "rev-parse", "HEAD")
    with pytest.raises(SwarmError, match="unrelated tracked changes remain: other.txt;"):
        publish.publish("swarm", p.agent, str(p.repo), p.ledger, "Merge dev", ["chosen.txt"])
    assert git(p.root, "rev-parse", "HEAD") == before
    assert (p.root / "other.txt").read_text() == "unrelated edit"


def test_publish_requires_conflict_resolutions_in_the_selected_scope(conflicted_publication):
    from scripts.swarm import publish
    from scripts.swarm.store import SwarmError

    p = conflicted_publication
    with pytest.raises(SwarmError, match="unrelated tracked changes remain: chosen.txt;"):
        publish.publish("swarm", p.agent, str(p.repo), p.ledger, "Merge dev", ["other.txt"])


def test_publish_does_not_exempt_files_from_a_merge_outside_dev(conflicted_publication):
    from scripts.swarm import publish
    from scripts.swarm.store import SwarmError

    p = conflicted_publication
    base = git(p.root, "merge-base", "HEAD", "MERGE_HEAD")
    git(p.root, "update-ref", "refs/remotes/origin/dev", base)
    with pytest.raises(SwarmError, match="unrelated tracked changes remain: inherited.txt, other.txt, sub/other.txt;"):
        publish.publish("swarm", p.agent, str(p.repo), p.ledger, "Merge another branch", ["chosen.txt"])


@pytest.mark.parametrize("fixture", ["publication", "conflicted_publication"])
def test_publish_requires_both_paths_of_a_staged_rename(request, fixture):
    from scripts.swarm import publish
    from scripts.swarm.store import SwarmError

    p = request.getfixturevalue(fixture)
    git(p.root, "mv", "other.txt", "renamed.txt")
    with pytest.raises(SwarmError, match="unrelated tracked changes remain: other.txt;"):
        publish.publish("swarm", p.agent, str(p.repo), p.ledger, "Rename file", ["chosen.txt", "renamed.txt"])


def test_publish_requires_the_source_of_an_unstaged_rename(conflicted_publication):
    from scripts.swarm import publish
    from scripts.swarm.store import SwarmError

    p = conflicted_publication
    (p.root / "other.txt").rename(p.root / "renamed.txt")
    git(p.root, "add", "-N", "renamed.txt")
    with pytest.raises(SwarmError, match="unrelated tracked changes remain: other.txt;"):
        publish.publish("swarm", p.agent, str(p.repo), p.ledger, "Rename file", ["chosen.txt", "renamed.txt"])


def test_publish_missing_dev_ref_refuses_without_leaking_git_diagnostics(conflicted_publication, capfd):
    from scripts.swarm import publish
    from scripts.swarm.store import SwarmError

    p = conflicted_publication
    git(p.root, "update-ref", "-d", "refs/remotes/origin/dev")
    with pytest.raises(SwarmError, match="unrelated tracked changes remain: inherited.txt, other.txt, sub/other.txt;"):
        publish.publish("swarm", p.agent, str(p.repo), p.ledger, "Merge dev", ["chosen.txt"])
    assert capfd.readouterr().err == ""


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


def counted_pushes(publish, monkeypatch):
    pushes = []
    native = publish.execute

    def counted(argv, where):
        if argv[:2] == ["git", "push"]:
            pushes.append(argv)
        return native(argv, where)

    monkeypatch.setattr(publish, "execute", counted)
    return pushes


@pytest.mark.parametrize("files", [[], ["chosen.txt"]])
def test_an_unchanged_publish_to_a_matching_remote_makes_no_commit_gate_or_push(publication, monkeypatch, files):
    from scripts.swarm import publish

    root = publication.root
    publish.publish(
        "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
    )
    head = git(root, "rev-parse", "HEAD")
    (root / ".git-gate-head").unlink()
    pushes = counted_pushes(publish, monkeypatch)
    result = publish.publish(
        "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", files
    )
    assert pushes == []
    assert git(root, "rev-parse", "HEAD") == head
    assert not (root / ".git-gate-head").exists()
    assert result["head"] == head
    assert (
        result["unchanged"]
        == "no changes in the listed files and the remote branch already points at this head; nothing pushed"
    )
    assert "gate" not in result


def test_an_unchanged_publish_still_pushes_a_branch_the_remote_is_behind(publication, monkeypatch):
    from scripts.swarm import publish

    root = publication.root
    publish.publish(
        "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
    )
    git(root, "commit", "--allow-empty", "-qm", "Local only")
    head = git(root, "rev-parse", "HEAD")
    pushes = counted_pushes(publish, monkeypatch)
    result = publish.publish(
        "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
    )
    assert len(pushes) == 1
    assert "unchanged" not in result
    assert git(root, "ls-remote", "origin", "refs/heads/engineer-test").split()[0] == head


def test_an_unchanged_publish_still_pushes_a_branch_the_remote_lacks(publication, monkeypatch):
    from scripts.swarm import publish

    root = publication.root
    publish.publish(
        "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
    )
    git(root, "push", "origin", "--delete", "engineer-test")
    head = git(root, "rev-parse", "HEAD")
    pushes = counted_pushes(publish, monkeypatch)
    result = publish.publish("swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", [])
    assert pushes == [["git", "push", "-u", "origin", "HEAD:refs/heads/engineer-test"]]
    assert "unchanged" not in result
    assert git(root, "ls-remote", "origin", "refs/heads/engineer-test").split()[0] == head
    assert publish.remote_head(root, "engineer-absent") == ""


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
        redis=SimpleNamespace(hget=lambda *_: None),
        key=lambda slug, tail: f"{slug}:{tail}",
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


@pytest.mark.parametrize("command", ["prepare", "publish"])
@pytest.mark.parametrize(
    "recording,branch",
    [
        ("map", "recorded-delivery"),
        ("legacy", "engineer-test"),
        ("unrecorded", ""),
        ("missing", ""),
        ("lookup-refused", ""),
    ],
)
def test_cli_journals_each_publication_refusal_with_task_branch_and_reason(
    publication, monkeypatch, command, recording, branch
):
    import syslog

    from scripts.swarm import cli, publish

    p = publication
    if recording == "map":
        p.task["repository_branches"] = {str(p.repo): "recorded-delivery"}
    if recording == "unrecorded":
        p.task.pop("branch")
    if recording == "missing":
        monkeypatch.setattr(p.ledger, "tasks", lambda slug: [])
    reason = (
        "publication requires this agent's claimed task" if recording == "missing" else "recorded branch is missing"
    )
    monkeypatch.setattr(cli, "_worker", lambda store, args: p.agent)
    monkeypatch.setattr(cli, "_local_repo", lambda store, slug: str(p.repo))
    monkeypatch.setattr(cli, "LedgerClient", lambda: p.ledger)
    opened, logged = [], []
    monkeypatch.setattr(syslog, "openlog", lambda ident: opened.append(ident))
    monkeypatch.setattr(syslog, "syslog", lambda priority, message: logged.append((priority, json.loads(message))))

    def refuse(*args):
        raise publish.SwarmError("recorded branch is missing")

    if recording == "lookup-refused":
        monkeypatch.setattr(p.ledger, "tasks", refuse)
    elif recording != "missing":
        monkeypatch.setattr(publish, command, refuse)
    args = SimpleNamespace(slug="swarm", command=command, message="Continue", files=["chosen.txt"])
    with pytest.raises(publish.SwarmError, match=reason):
        getattr(cli, f"cmd_{command}")(None, args)
    assert opened == ["agentihooks-swarm"]
    assert logged == [
        (
            syslog.LOG_WARNING,
            {
                "event": "swarm_publication_refused",
                "slug": "swarm",
                "command": command,
                "task": "t1",
                "branch": branch,
                "reason": reason,
            },
        )
    ]


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


def test_continuation_publishes_its_current_branch(publication):
    from scripts.swarm import publish

    root = publication.root
    git(root, "push", "origin", "HEAD:predecessor")
    publication.task["branch"] = "engineer-test"
    result = publish.publish(
        "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
    )
    head = git(root, "rev-parse", "HEAD")
    assert result["branch"] == publication.task["branch"] == "engineer-test"
    assert git(root, "ls-remote", "origin", "refs/heads/engineer-test").split()[0] == head
    assert git(root, "ls-remote", "origin", "refs/heads/predecessor").split()[0] != head
    assert git(root, "rev-parse", "--abbrev-ref", "@{upstream}") == "origin/engineer-test"


@pytest.mark.parametrize("cwd", ["worktree", "outside"])
def test_stacked_delivery_publishes_its_current_branch_with_one_push(publication, monkeypatch, cwd):
    from scripts.swarm import publish

    root = publication.root
    git(root, "push", "origin", "HEAD:predecessor")
    pr_url = "https://github.com/example/repo/pull/7"
    publication.task.update(branch="predecessor", pr_url=pr_url, state="pr")
    if cwd == "worktree":
        monkeypatch.chdir(root)
    pushes = []
    native = publish.execute

    def counted(argv, where):
        if argv[:2] == ["git", "push"]:
            pushes.append(argv)
        return native(argv, where)

    monkeypatch.setattr(publish, "execute", counted)
    publish.prepare("swarm", publication.agent, str(publication.repo), publication.ledger)
    assert publication.task["branch"] == "engineer-test"
    for name in ("chosen.txt", "other.txt"):
        (root / name).write_text("after")
        pushes.clear()
        result = publish.publish(
            "swarm", publication.agent, str(publication.repo), publication.ledger, f"Change {name}", [name]
        )
        head = git(root, "rev-parse", "HEAD")
        assert len(pushes) == 1
        assert result["branch"] == publication.task["branch"] == "engineer-test"
        assert publication.task["repository_branches"] == {str(publication.repo): "engineer-test"}
        assert publication.task["pr_url"] == pr_url
        assert git(root, "ls-remote", "origin", "refs/heads/engineer-test").split()[0] == head
        assert git(root, "ls-remote", "origin", "refs/heads/predecessor").split()[0] != head
    assert git(root, "branch", "--show-current") == "engineer-test"


@pytest.mark.parametrize(
    "pull",
    [
        {},
        {"state": "CLOSED"},
        {"state": "MERGED"},
        {"isCrossRepository": True},
        {"repository": {"nameWithOwner": "example/other"}},
        {"headRefName": "dev"},
        {"behind": True},
    ],
)
def test_a_continuation_publishes_its_registered_branch_regardless_of_its_pull_request(publication, monkeypatch, pull):
    from scripts.swarm import merge_queue, publish

    root, repository = publication.root, publication.repo
    pr_url = "https://github.com/example/repo/pull/7"
    parked = git(root, "rev-parse", "HEAD")
    git(root, "push", "origin", "HEAD:predecessor")
    publication.task.update(pr_url=pr_url, state="pr")
    monkeypatch.chdir(root)
    native = publish.git
    pushes, fetches = [], []

    def git_call(where, *args):
        if args == ("remote", "get-url", "--push", "origin"):
            return "git@github.com:Example/Repo.git"
        if args[:1] == ("push",):
            pushes.append(args)
        if args[:1] == ("fetch",):
            fetches.append(args)
        return native(where, *args)

    def query(document, variables):
        if "resource" in document:
            return {"resource": {"state": "OPEN", "headRefName": "predecessor", **pull}}
        assert variables == {"owner": "Example", "name": "Repo", "branch": "engineer-test"}
        return {"repository": {"pullRequests": {"nodes": [], "pageInfo": {"hasNextPage": False}}}}

    monkeypatch.setattr(publish, "git", git_call)
    monkeypatch.setattr(merge_queue, "graphql", query)
    result = publish.publish(
        "swarm", publication.agent, str(repository), publication.ledger, "Change chosen", ["chosen.txt"]
    )

    head = git(root, "rev-parse", "HEAD")
    assert len(pushes) == 1
    assert fetches == []
    assert result["branch"] == publication.task["branch"] == "engineer-test"
    assert publication.task["repository_branches"] == {str(repository): "engineer-test"}
    assert publication.task["pr_url"] == pr_url
    assert git(root, "ls-remote", "origin", "refs/heads/engineer-test").split()[0] == head
    assert git(root, "ls-remote", "origin", "refs/heads/predecessor").split()[0] == parked
    assert git(root, "rev-parse", "--abbrev-ref", "@{upstream}") == "origin/engineer-test"


def test_a_pull_request_on_another_host_is_never_looked_up(monkeypatch):
    from scripts.swarm import merge_queue, publish_queue

    monkeypatch.setattr(merge_queue, "graphql", lambda *args: pytest.fail("looked up"))
    assert publish_queue.open_head("/srv/remote.git", "https://github.com/example/repo/pull/7") == ""
    assert publish_queue.open_head("git@github.com:example/repo.git", "") == ""


def test_a_recorded_url_that_names_no_pull_request_keeps_the_current_branch(monkeypatch):
    from scripts.swarm import merge_queue, publish_queue

    monkeypatch.setattr(merge_queue, "graphql", lambda document, variables: {"resource": None})
    assert publish_queue.open_head("git@github.com:example/repo.git", "https://github.com/example/repo/issues/7") == ""


def test_a_missing_branch_never_descends_and_prints_nothing(publication, capfd):
    from scripts.swarm import publish

    capfd.readouterr()
    assert publish.descends(publication.root, "missing") is False
    assert capfd.readouterr() == ("", "")


def test_prepare_records_the_worktree_branch_when_it_does_not_descend_from_the_pull_request(publication):
    from scripts.swarm import publish

    repository = publication.repo
    parked = git(repository, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "Parked predecessor work")
    git(repository, "push", "origin", f"{parked}:refs/heads/predecessor")
    git(repository, "fetch", "origin")
    publication.task.update(branch="predecessor", pr_url="https://github.com/example/repo/pull/7", state="pr")

    result = publish.prepare("swarm", publication.agent, str(repository), publication.ledger)

    assert result["branch"] == publication.task["branch"] == "engineer-test"
    assert publication.task["repository_branches"] == {str(repository): "engineer-test"}


def test_prepare_and_publish_keep_the_open_pull_requests_delivery_branch(publication, monkeypatch):
    from scripts.swarm import publish

    p = publication
    git(p.root, "push", "origin", "HEAD:predecessor")
    p.task.update(
        branch="predecessor",
        repository_branches={str(p.repo): "predecessor"},
        pr_url="https://github.com/example/repo/pull/7",
    )

    def open_head(remote, url):
        assert remote == str(p.repo.parent / "remote.git")
        assert url == "https://github.com/example/repo/pull/7"
        return "predecessor"

    monkeypatch.setattr(publish.publish_queue, "open_head", open_head)
    result = publish.prepare("swarm", p.agent, str(p.repo), p.ledger)
    assert result["worktree"] == str(p.root)
    assert git(p.root, "branch", "--show-current") == "engineer-test"
    assert p.task["branch"] == "predecessor"
    assert p.task["repository_branches"] == {str(p.repo): "predecessor"}
    monkeypatch.chdir(p.repo)
    published = publish.publish("swarm", p.agent, str(p.repo), p.ledger, "Continue pull request", ["chosen.txt"])
    assert published["branch"] == p.task["branch"] == "predecessor"
    assert git(p.repo, "ls-remote", "origin", "refs/heads/predecessor").split()[0] == published["head"]
    assert git(p.repo, "ls-remote", "origin", "refs/heads/engineer-test") == ""


@pytest.mark.parametrize("open_branch,diverged", [("", False), ("another-task", False), ("predecessor", True)])
def test_publish_refuses_unverified_or_diverged_retained_delivery(publication, monkeypatch, open_branch, diverged):
    from scripts.swarm import publish

    p = publication
    commit = git(p.root, "rev-parse", "HEAD")
    if diverged:
        commit = git(p.root, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "Unrelated retained work")
    git(p.root, "push", "origin", f"{commit}:refs/heads/predecessor")
    p.task.update(branch="predecessor", pr_url="https://github.com/example/repo/pull/7")
    monkeypatch.setattr(publish.publish_queue, "open_head", lambda remote, url: open_branch)
    monkeypatch.chdir(p.root)
    before = git(p.root, "rev-parse", "HEAD")
    with pytest.raises(publish.SwarmError, match="differs from registered branch predecessor"):
        publish.publish("swarm", p.agent, str(p.repo), p.ledger, "Keep unrelated work", ["chosen.txt"])
    assert git(p.root, "rev-parse", "HEAD") == before
    assert git(p.root, "diff", "--cached", "--name-only") == ""
    assert git(p.repo, "ls-remote", "origin", "refs/heads/predecessor").split()[0] == commit


@pytest.mark.parametrize("command", ["prepare", "publish"])
def test_missing_pull_request_never_authorizes_retained_delivery(publication, monkeypatch, command):
    from scripts.swarm import publish

    p = publication
    git(p.root, "push", "origin", "HEAD:predecessor")
    p.task["branch"] = "predecessor"

    def open_head(remote, url):
        assert remote == str(p.repo.parent / "remote.git")
        assert url == ""
        return ""

    monkeypatch.setattr(publish.publish_queue, "open_head", open_head)
    if command == "prepare":
        result = publish.prepare("swarm", p.agent, str(p.repo), p.ledger)
        assert result["branch"] == p.task["branch"] == "engineer-test"
    else:
        with pytest.raises(publish.SwarmError, match="differs from registered branch predecessor"):
            publish.publish("swarm", p.agent, str(p.repo), p.ledger, "Do not redirect", ["chosen.txt"])


def test_a_restacked_successor_publishes_its_prepared_branch_not_the_parked_one(publication):
    from scripts.swarm import publish

    root, repository = publication.root, publication.repo
    parked = git(repository, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "Parked predecessor work")
    git(repository, "push", "origin", f"{parked}:refs/heads/predecessor")
    publication.task["branch"] = "engineer-test"
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

    upstream = subprocess.run(["git", "-C", str(root), "rev-parse", "@{upstream}"], capture_output=True)
    assert upstream.returncode != 0
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
