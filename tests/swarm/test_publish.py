import json
import subprocess
import threading
import urllib.error
from collections.abc import Callable, Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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


@pytest.mark.feature("lab-remote-in-workspaces")
def test_a_lab_push_needs_the_prepush_gate_like_any_push(repository: Path) -> None:
    git(repository, "remote", "add", "lab", "http://lab.invalid/repo.git")
    with pytest.raises(BlockAction) as blocked:
        check_prepush(
            {"tool_input": {"command": "git push -o branch=dev lab HEAD:refs/grade/t1"}, "cwd": str(repository)}
        )
    assert f"Next: cd {repository} && python -m scripts.ci_prepush" in str(blocked.value)


@pytest.mark.feature("lab-remote-in-workspaces")
def test_the_stop_gate_counts_a_head_pushed_only_to_the_lab_as_unpushed(repository: Path, tmp_path: Path) -> None:
    import re

    from scripts.gates import push_stop

    lab = tmp_path / "lab.git"
    git(tmp_path, "init", "-q", "--bare", str(lab))
    git(repository, "commit", "-q", "--allow-empty", "-m", "graded only")
    git(repository, "push", "-q", str(lab), "HEAD:refs/grade/t1")
    tree = push_stop.inspect(repository, re.compile("dev"))
    assert tree is not None
    assert tree.unpushed == 1


def _no_collection(slug):
    pytest.fail("publication read the whole tasks collection")


def _by_id(task, slug="swarm"):
    return lambda asked, task_id: task if (asked, task_id) == (slug, task["id"]) else None


@pytest.fixture
def publication(repository, tmp_path, monkeypatch):
    worktree = tmp_path / "worktrees" / "repo" / "engineer-test"
    git(repository, "worktree", "add", "-b", "engineer-test", str(worktree))
    task = {"id": "t1", "claimed_by": "engineer@test", "state": "claimed", "branch": "engineer-test"}
    updates = []

    def update(slug, task_id, fields, by):
        updates.append((slug, task_id, fields, by))
        task.update(fields)

    ledger = SimpleNamespace(task=_by_id(task), tasks=_no_collection, update_task=update)
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
    original = git(root, "rev-parse", "HEAD")
    diverged = git(root, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "Prerequisite work")
    git(root, "push", "origin", f"{diverged}:refs/heads/prerequisite")
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
    assert git(root, "ls-remote", "origin", "refs/heads/prerequisite").split()[0] == diverged
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
        task=_by_id(task), tasks=_no_collection, update_task=lambda slug, task_id, fields, by: task.update(fields)
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
        task=_by_id(task), tasks=_no_collection, update_task=lambda slug, task_id, fields, by: task.update(fields)
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


@pytest.mark.feature("prepare-unpushed-branch-fresh-cut")
@pytest.mark.parametrize("hint", ["", "handoff_envelope", "reclaim"])
def test_prepare_cuts_fresh_dev_when_the_recorded_branch_was_never_pushed(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hint: str
) -> None:
    from scripts.swarm import publish, stack

    fresh = git(repository, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "New dev")
    git(repository, "push", "origin", f"{fresh}:refs/heads/dev")
    worktree = tmp_path / "worktrees" / "repo" / "engineer-test"
    script = tmp_path / "wt.sh"
    script.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'git -C "$3" fetch origin dev >&2\n'
        "base=origin/dev\n"
        'if [[ "${4:-}" == --from ]]; then git -C "$3" fetch origin "${5#origin/}" >&2; base="$5"; fi\n'
        'git -C "$3" worktree add --no-track -b engineer-test "$PUBLISH_PREPARED" "$base" >&2\n'
        'printf "%s\\n" "$PUBLISH_PREPARED"\n'
    )
    monkeypatch.setattr(stack, "WT_SCRIPT", script)
    monkeypatch.setenv("PUBLISH_PREPARED", str(worktree))
    task = {
        "id": "t1",
        "claimed_by": "engineer@test",
        "state": "claimed",
        "branch": "never-pushed",
        "repository_branches": {str(repository): "never-pushed"},
    }
    if hint:
        task[hint] = {"continue_from": "origin/never-pushed"}
    ledger: Any = SimpleNamespace(
        task=_by_id(task), tasks=_no_collection, update_task=lambda slug, task_id, fields, by: task.update(fields)
    )
    agent = AgentRecord("engineer@test", "ci", "t1")

    result = publish.prepare("swarm", agent, str(repository), ledger)

    assert result["worktree"] == str(worktree)
    assert git(worktree, "rev-parse", "HEAD") == fresh
    assert result["branch"] == task["branch"] == "engineer-test"
    assert task["repository_branches"] == {str(repository): "engineer-test"}
    upstream = subprocess.run(["git", "-C", str(worktree), "rev-parse", "@{upstream}"], capture_output=True)
    assert upstream.returncode != 0


@pytest.mark.parametrize(
    "state, deleted, head",
    [
        ("MERGED", True, "predecessor"),
        ("MERGED", False, "predecessor"),
        ("OPEN", False, "predecessor"),
        ("MERGED", False, "another-branch"),
    ],
)
@pytest.mark.parametrize(
    "continuation",
    [(hint, continued) for hint in ("handoff_envelope", "reclaim") for continued in ("predecessor", "kept")],
)
def test_prepare_selects_fresh_dev_after_the_recorded_pull_request_merged(
    repository, tmp_path, monkeypatch, state, deleted, head, continuation
):
    from scripts.swarm import publish, stack
    from scripts.swarm_ledger import ledger_tasks

    hint, continued = continuation

    git(repository, "push", "origin", "HEAD:predecessor")
    git(repository, "push", "origin", "HEAD:kept")
    old_head = git(repository, "rev-parse", "HEAD")
    fresh = git(repository, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "New dev")
    git(repository, "push", "origin", f"{fresh}:refs/heads/dev")
    if deleted:
        git(repository, "push", "origin", "--delete", "predecessor")
    worktree = tmp_path / "worktrees" / "repo" / "engineer-test"
    script = tmp_path / "wt.sh"
    script.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'git -C "$3" fetch origin dev >&2\n'
        "base=origin/dev\n"
        'if [[ "${4:-}" == --from ]]; then base="$5"; fi\n'
        'git -C "$3" worktree add -b engineer-test "$PUBLISH_PREPARED" "$base" >&2\n'
        'printf "%s\\n" "$PUBLISH_PREPARED"\n'
    )
    monkeypatch.setattr(stack, "WT_SCRIPT", script)
    monkeypatch.setenv("PUBLISH_PREPARED", str(worktree))
    pr_url = "https://github.com/example/repo/pull/7"
    task = {
        "id": "t1",
        "claimed_by": "engineer@test",
        "state": "claimed",
        "branch": "predecessor",
        "pr_url": pr_url,
        "repository_branches": {str(repository): "predecessor"},
        hint: {"continue_from": f"origin/{continued}", "source": "origin/predecessor"},
    }
    native_git = publish.git

    def git_call(root, *args):
        if args == ("remote", "get-url", "origin"):
            return "git@github.com:example/repo.git"
        return native_git(root, *args)

    monkeypatch.setattr(publish, "git", git_call)

    from scripts.tick.ports.source_control import ChangeRef, ChangeState

    def change(url: str, refused: str) -> ChangeState:
        assert (url, refused) == (pr_url, f"cannot read pull request {pr_url}")
        lowered = "open" if state == "OPEN" else "closed"
        return ChangeState(ChangeRef("example/repo", 7), lowered, "a" * 40, "dev", state == "MERGED", url, head)

    monkeypatch.setattr(publish.stack, "change", change)
    monkeypatch.setattr(publish.publish_queue, "open_head", lambda remote, url: head if state == "OPEN" else "")

    def update_task(slug, task_id, fields, by):
        ledger_tasks.check({"op": "task_update", "item": f"tasks/{task_id}", "fields": fields, "by": by})
        task.update(fields)

    ledger = SimpleNamespace(task=_by_id(task, slug="swarm"), tasks=_no_collection, update_task=update_task)
    agent = AgentRecord("engineer@test", "ci", "t1")

    result = publish.prepare("swarm", agent, str(repository), ledger)

    retired = state == "MERGED" and head == "predecessor"
    registered = "predecessor" if state == "OPEN" else "engineer-test"
    upstream = "origin/dev" if retired else "origin/predecessor"
    assert git(worktree, "rev-parse", "HEAD") == (fresh if retired else old_head)
    assert git(worktree, "rev-parse", "--abbrev-ref", "@{upstream}") == (
        "origin/kept" if continued == "kept" and state != "OPEN" else upstream
    )
    assert result["branch"] == task["branch"] == registered
    assert task["repository_branches"] == {str(repository): registered}
    assert task["pr_url"] == ("" if retired else pr_url)
    assert task[hint] == {"continue_from": f"origin/{continued}", "source": "origin/predecessor"}
    assert publish.prepare("swarm", agent, str(repository), ledger) == result


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
        task=_by_id(task), tasks=_no_collection, update_task=lambda slug, task_id, fields, by: task.update(fields)
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
        task=_by_id(task), tasks=_no_collection, update_task=lambda slug, task_id, fields, by: task.update(fields)
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
        task=_by_id(task), tasks=_no_collection, update_task=lambda slug, task_id, fields, by: task.update(fields)
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

    publication.ledger.task = lambda slug, task_id: None
    with pytest.raises(SwarmError, match="^publication requires this agent's claimed task$"):
        publish.publish(
            "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
        )


@pytest.mark.parametrize("command", ["prepare", "publish"])
def test_publication_reads_its_own_task_by_id_and_never_the_tasks_collection(publication, monkeypatch, command):
    from scripts.swarm import cli

    reads, by_id = [], publication.ledger.task

    def task(slug, task_id):
        reads.append((slug, task_id))
        return by_id(slug, task_id)

    publication.ledger.task = task
    monkeypatch.setattr(cli, "_worker", lambda store, args: publication.agent)
    monkeypatch.setattr(cli, "_local_repo", lambda store, slug: str(publication.repo))
    monkeypatch.setattr(cli, "LedgerClient", lambda: publication.ledger)
    monkeypatch.chdir(publication.repo)
    args = SimpleNamespace(
        slug="swarm", command=command, message="Change chosen", files=["chosen.txt"], via="", direct=False
    )
    getattr(cli, f"cmd_{command}")(None, args)
    assert reads and set(reads) == {("swarm", "t1")}


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
        monkeypatch.setattr(p.ledger, "task", lambda slug, task_id: None)
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
        monkeypatch.setattr(p.ledger, "task", refuse)
    elif recording != "missing":
        monkeypatch.setattr(publish, command, refuse)
    args = SimpleNamespace(
        slug="swarm", command=command, message="Continue", files=["chosen.txt"], via="", direct=False
    )
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


@pytest.mark.parametrize(
    "pull, expected",
    [
        ({"state": "OPEN", "isCrossRepository": False, "repository": {"nameWithOwner": "Example/Repo"}}, "task"),
        ({"state": "MERGED", "isCrossRepository": False, "repository": {"nameWithOwner": "example/repo"}}, ""),
        ({"state": "OPEN", "isCrossRepository": True, "repository": {"nameWithOwner": "example/repo"}}, ""),
        ({"state": "OPEN", "isCrossRepository": False, "repository": {"nameWithOwner": "example/other"}}, ""),
    ],
)
def test_open_head_names_only_an_open_pull_request_in_the_same_repository(monkeypatch, pull, expected):
    from scripts.swarm import merge_queue, publish_queue

    url = "https://github.com/example/repo/pull/7"

    def graphql(document, variables):
        assert document == publish_queue.HEAD
        assert variables == {"url": url}
        return {"resource": {**pull, "headRefName": "task"}}

    monkeypatch.setattr(merge_queue, "graphql", graphql)
    assert publish_queue.open_head("git@github.com:example/repo.git", url) == expected


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


def test_prepare_registers_the_open_pull_requests_head_over_the_successor_branch(publication, monkeypatch):
    from scripts.swarm import publish

    p = publication
    git(p.root, "push", "origin", "HEAD:pull-head")
    p.task.update(pr_url="https://github.com/example/repo/pull/7", state="pr")
    monkeypatch.setattr(publish.publish_queue, "open_head", lambda remote, url: "pull-head" if url else "")

    result = publish.prepare("swarm", p.agent, str(p.repo), p.ledger)

    assert result["branch"] == p.task["branch"] == "pull-head"
    assert p.task["repository_branches"] == {str(p.repo): "pull-head"}
    assert git(p.root, "rev-parse", "--abbrev-ref", "@{upstream}") == "origin/pull-head"


def test_prepare_refuses_an_open_pull_request_head_the_worktree_does_not_descend_from(publication, monkeypatch):
    from scripts.swarm import publish

    p = publication
    diverged = git(p.root, "commit-tree", "HEAD^{tree}", "-p", "origin/dev", "-m", "Unrelated pull request work")
    git(p.root, "push", "origin", f"{diverged}:refs/heads/pull-head")
    p.task.update(pr_url="https://github.com/example/repo/pull/7", state="pr")
    monkeypatch.setattr(publish.publish_queue, "open_head", lambda remote, url: "pull-head" if url else "")

    with pytest.raises(publish.SwarmError, match="delivers branch pull-head.*on branch engineer-test"):
        publish.prepare("swarm", p.agent, str(p.repo), p.ledger)
    assert p.task["branch"] == "engineer-test"
    assert p.updates == []


@pytest.mark.parametrize("checkout", ["recorded", "own"])
def test_a_fresh_continuation_on_the_recorded_branch_publishes_past_an_unpushed_predecessor_entry(
    publication, monkeypatch, checkout
):
    from scripts.swarm import publish

    p = publication
    continuation = p.root
    if checkout == "recorded":
        continuation = p.repo.parent / "worktrees" / "repo" / "recorded"
        git(p.repo, "worktree", "add", "-b", "recorded", str(continuation), "dev")
        git(continuation, "push", "-u", "origin", "recorded")
        (continuation / "scripts" / "ci_prepush" / "__main__.py").write_text(
            (p.root / "scripts" / "ci_prepush" / "__main__.py").read_text()
        )
        git(continuation, "add", "scripts/ci_prepush/__main__.py")
        git(continuation, "commit", "-qm", "Install gate fixture")
        (continuation / "chosen.txt").write_text("after")
    else:
        git(continuation, "push", "origin", "HEAD:recorded")
    p.task.update(
        branch="recorded",
        repository_branches={str(p.repo): "predecessor"},
        pr_url="https://github.com/example/repo/pull/7",
    )
    monkeypatch.setattr(publish.publish_queue, "open_head", lambda remote, url: "recorded")
    monkeypatch.chdir(continuation)
    published = publish.publish("swarm", p.agent, str(p.repo), p.ledger, "Continue pull request", ["chosen.txt"])
    assert published["branch"] == p.task["branch"] == "recorded"
    assert p.task["repository_branches"] == {str(p.repo): "recorded"}
    assert git(p.repo, "ls-remote", "origin", "refs/heads/recorded").split()[0] == published["head"]
    assert git(p.repo, "ls-remote", "origin", "refs/heads/predecessor") == ""


@pytest.mark.parametrize(
    "pushed,expected", [((), "predecessor"), (("recorded",), "recorded"), (("predecessor", "recorded"), "predecessor")]
)
def test_continuation_takes_the_recorded_branch_only_over_an_unpushed_entry(publication, pushed, expected):
    from scripts.swarm import publish

    p = publication
    for name in pushed:
        git(p.root, "push", "origin", f"HEAD:{name}")
    task = {**p.task, "branch": "recorded", "branch_repo": str(p.repo.parent / "remote.git")}
    scoped = {**task, "branch": "predecessor"}
    assert publish.continuation_task(p.repo, task, scoped, "engineer-test")["branch"] == expected
    assert publish.continuation_task(p.repo, task, scoped, "predecessor") == scoped


@pytest.mark.parametrize("open_branch,diverged", [("", True), ("another-task", False), ("predecessor", True)])
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
def test_missing_pull_request_publishes_the_successor_branch_and_keeps_the_retained_one(
    publication, monkeypatch, command
):
    from scripts.swarm import publish

    p = publication
    git(p.root, "push", "origin", "HEAD:predecessor")
    retained = git(p.root, "rev-parse", "HEAD")
    p.task["branch"] = "predecessor"

    def open_head(remote, url):
        assert remote == str(p.repo.parent / "remote.git")
        assert url == ""
        return ""

    monkeypatch.setattr(publish.publish_queue, "open_head", open_head)
    monkeypatch.chdir(p.repo)
    if command == "prepare":
        result = publish.prepare("swarm", p.agent, str(p.repo), p.ledger)
    else:
        result = publish.publish("swarm", p.agent, str(p.repo), p.ledger, "Do not redirect", ["chosen.txt"])
    assert result["branch"] == p.task["branch"] == "engineer-test"
    assert git(p.repo, "ls-remote", "origin", "refs/heads/predecessor").split()[0] == retained


@pytest.mark.parametrize("command", ["prepare", "publish"])
def test_hive_successor_publishes_its_own_branch_over_the_predecessor_hive_key(publication, monkeypatch, command):
    from scripts.swarm import publish

    p = publication
    git(p.root, "push", "origin", "HEAD:predecessor")
    hive_key = f"/hive/predecessor/{p.repo.name}"
    older = f"/hive/older/{p.repo.name}"
    p.task.update(
        branch="predecessor",
        repository_branches={hive_key: "predecessor", older: "older-task", "/hive/bundle": "kept"},
    )
    monkeypatch.setattr(publish.publish_queue, "open_head", lambda remote, url: "")
    monkeypatch.chdir(p.repo)

    if command == "prepare":
        result = publish.prepare("swarm", p.agent, str(p.repo), p.ledger)
    else:
        result = publish.publish("swarm", p.agent, str(p.repo), p.ledger, "Continue as successor", ["chosen.txt"])
        assert git(p.repo, "ls-remote", "origin", "refs/heads/engineer-test").split()[0] == result["head"]

    assert result["branch"] == p.task["branch"] == "engineer-test"
    assert p.task["repository_branches"] == {str(p.repo): "engineer-test", "/hive/bundle": "kept"}
    assert git(p.root, "rev-parse", "--abbrev-ref", "HEAD") == "engineer-test"


@pytest.mark.parametrize("command", ["mapped", "scoped", "prepare", "publish"])
@pytest.mark.parametrize("local_stale", [True, False])
def test_conflicting_host_branches_keep_the_open_pull_delivery(
    publication: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, command: str, local_stale: bool
) -> None:
    from scripts.swarm import publish

    p = publication
    git(p.root, "push", "origin", "HEAD:pull-head")
    git(p.root, "push", "origin", "HEAD:stale-branch")
    foreign = f"/workstation/dev/{p.repo.name}"
    stale_key = str(p.repo) if local_stale else f"/hive/previous/{p.repo.name}"
    p.task.update(
        branch="pull-head",
        repository_branches={stale_key: "stale-branch", foreign: "pull-head", "/hive/bundle": "kept"},
        pr_url="https://github.com/example/repo/pull/7",
    )

    def open_head(remote: str, url: str) -> str:
        assert remote == str(p.repo.parent / "remote.git")
        assert url == p.task["pr_url"]
        return "pull-head"

    monkeypatch.setattr(publish.publish_queue, "open_head", open_head)
    monkeypatch.chdir(p.root)
    if command == "mapped":
        assert publish.mapped_branch(p.repo, p.task) == "pull-head"
    elif command == "scoped":
        assert publish.repository_task(p.repo, p.task)["branch"] == "pull-head"
    else:
        if command == "prepare":
            result = publish.prepare("swarm", p.agent, str(p.repo), p.ledger)
        else:
            result = publish.publish("swarm", p.agent, str(p.repo), p.ledger, "Continue delivery", ["chosen.txt"])
            assert git(p.repo, "ls-remote", "origin", "refs/heads/pull-head").split()[0] == result["head"]
            assert git(p.repo, "ls-remote", "origin", "refs/heads/engineer-test") == ""
        assert result["branch"] == p.task["branch"] == "pull-head"
        assert p.task["repository_branches"] == {str(p.repo): "pull-head", "/hive/bundle": "kept"}


@pytest.mark.parametrize(
    ("branches", "url", "head", "expected", "queries"),
    [
        ({"repo": "local", "foreign/repo": "other"}, "pull", "", "local", 1),
        ({"foreign/repo": "first", "elsewhere/repo": "second"}, "pull", "", "first", 1),
        ({"repo": "local", "foreign/repo": "other"}, "", "unexpected", "local", 0),
        ({"repo": "same", "foreign/repo": "same"}, "pull", "unexpected", "same", 0),
        ({"repo": "local", "foreign/other": "other"}, "pull", "unexpected", "local", 0),
        ({"foreign/repo": "first"}, "pull", "unexpected", "first", 0),
        ({}, "pull", "unexpected", None, 0),
        ({"repo": "local", "foreign/repo": "other"}, "pull", "new-head", "new-head", 1),
    ],
)
def test_branch_mapping_only_queries_conflicting_delivery_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    branches: dict[str, str],
    url: str,
    head: str,
    expected: str | None,
    queries: int,
) -> None:
    from scripts.swarm import publish

    repo = tmp_path / "repo"
    calls = []

    def git_call(root: Path, *args: str) -> str:
        assert root == repo
        assert args == ("remote", "get-url", "origin")
        return "git@github.com:example/repo.git"

    def open_head(remote: str, pull: str) -> str:
        calls.append((remote, pull))
        return head

    monkeypatch.setattr(publish, "git", git_call)
    monkeypatch.setattr(publish.publish_queue, "open_head", open_head)
    mapping = {str(tmp_path / key): value for key, value in branches.items()}
    assert publish.mapped_branch(repo, {"repository_branches": mapping, "pr_url": url}) == expected
    assert calls == [("git@github.com:example/repo.git", url)] * queries


@pytest.mark.parametrize(("state", "merged"), [("open", False), ("closed", False), ("closed", True)])
def test_successor_publishes_past_a_settled_pull_request_only(
    publication: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, state: str, merged: bool
) -> None:
    from scripts.swarm import publish
    from scripts.tick.ports.source_control import ChangeRef, ChangeState

    p = publication
    git(p.root, "push", "origin", "HEAD:predecessor")
    url = "https://github.com/example/repo/pull/7"
    p.task.update(branch="predecessor", pr_url=url)
    monkeypatch.setattr(publish.publish_queue, "open_head", lambda remote, pull: "")
    viewed = []

    def change(asked: str, refused: str) -> ChangeState:
        assert (asked, refused) == (url, f"cannot read pull request {url}")
        viewed.append(asked)
        return ChangeState(ChangeRef("example/repo", 7), state, "a" * 40, "dev", merged, url)

    monkeypatch.setattr(publish.stack, "change", change)
    monkeypatch.chdir(p.root)
    if state == "open":
        with pytest.raises(publish.SwarmError, match="differs from registered branch predecessor"):
            publish.publish("swarm", p.agent, str(p.repo), p.ledger, "Continue as successor", ["chosen.txt"])
        assert p.updates == []
    else:
        result = publish.publish("swarm", p.agent, str(p.repo), p.ledger, "Continue as successor", ["chosen.txt"])
        assert result["branch"] == p.task["branch"] == "engineer-test"
    assert len(viewed) == 1


def _pull_request(monkeypatch: pytest.MonkeyPatch, state: str) -> list[str]:
    from scripts.swarm import publish
    from scripts.tick.ports.source_control import ChangeRef, ChangeState

    viewed: list[str] = []

    def change(asked: str, refused: str) -> ChangeState:
        assert refused == f"cannot read pull request {asked}"
        viewed.append(asked)
        return ChangeState(ChangeRef("example/repo", 7), state, "a" * 40, "dev", False, asked)

    monkeypatch.setattr(publish.stack, "change", change)
    monkeypatch.setattr(publish.publish_queue, "open_head", lambda remote, url: "")
    return viewed


@pytest.mark.feature("publish-unpushed-predecessor-fresh-branch")
def test_publish_refuses_a_fresh_branch_while_the_recorded_branch_holds_commits_it_lacks(
    publication: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm import publish

    p = publication
    retained = git(p.root, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "Retained predecessor work")
    git(p.root, "push", "origin", f"{retained}:refs/heads/predecessor")
    p.task.update(branch="predecessor")
    _pull_request(monkeypatch, "closed")
    monkeypatch.chdir(p.root)
    before = git(p.root, "rev-parse", "HEAD")
    with pytest.raises(publish.SwarmError, match="differs from registered branch predecessor"):
        publish.publish("swarm", p.agent, str(p.repo), p.ledger, "Keep retained work", ["chosen.txt"])
    assert p.updates == []
    assert git(p.root, "rev-parse", "HEAD") == before
    assert git(p.repo, "ls-remote", "origin", "refs/heads/predecessor").split()[0] == retained
    assert git(p.repo, "ls-remote", "origin", "refs/heads/engineer-test") == ""


@pytest.mark.feature("publish-unpushed-predecessor-fresh-branch")
def test_publish_refuses_a_fresh_branch_over_a_never_pushed_branch_while_its_pull_request_is_open(
    publication: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm import publish

    p = publication
    p.task.update(branch="never-pushed", pr_url="https://github.com/example/repo/pull/7")
    _pull_request(monkeypatch, "open")
    monkeypatch.chdir(p.root)
    with pytest.raises(publish.SwarmError, match="differs from registered branch never-pushed"):
        publish.publish("swarm", p.agent, str(p.repo), p.ledger, "Wait for the open pull request", ["chosen.txt"])
    assert p.updates == []
    assert p.task["branch"] == "never-pushed"
    assert git(p.repo, "ls-remote", "origin", "refs/heads/engineer-test") == ""


@pytest.mark.feature("publish-unpushed-predecessor-fresh-branch")
@pytest.mark.parametrize("url", ["", "https://github.com/example/repo/pull/7"])
def test_publish_records_a_fresh_branch_over_a_never_pushed_recorded_branch(
    publication: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    from scripts.swarm import publish

    p = publication
    p.task.update(branch="never-pushed", pr_url=url)
    viewed = _pull_request(monkeypatch, "closed")
    monkeypatch.chdir(p.root)
    result = publish.publish("swarm", p.agent, str(p.repo), p.ledger, "Continue on a fresh branch", ["chosen.txt"])
    head = git(p.root, "rev-parse", "HEAD")
    assert result["branch"] == p.task["branch"] == "engineer-test"
    assert p.task["repository_branches"] == {str(p.repo): "engineer-test"}
    assert git(p.repo, "ls-remote", "origin", "refs/heads/engineer-test").split()[0] == head
    assert git(p.repo, "ls-remote", "origin", "refs/heads/never-pushed") == ""
    assert viewed == ([url] if url else [])


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


@pytest.mark.parametrize(
    ("stacked", "replayed", "failure"),
    [
        ("rebased", "2", ""),
        ("dependency", "1", ""),
        ("dependency-rebased", "1", ""),
        ("dependency-modified", "1", ""),
        ("dependency-spaced", "1", ""),
        ("dependency-modified", "1", "the task shares no history with dev"),
        ("dependency-modified", "1", "cannot resolve the completed rebase tree"),
        ("dependency-modified", "1", "the stacked base shares no history with dev"),
        ("dependency-modified", "1", "cannot list dependency files"),
        ("dependency-modified", "1", "cannot read completed dependency history"),
    ],
)
def test_a_restack_after_a_manual_rebase_replays_only_the_task_commits(
    publication: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, stacked: str, replayed: str, failure: str
) -> None:
    from scripts.swarm import stack

    p = publication
    root, repository = p.root, p.repo
    (root / "chosen.txt").write_text("before")
    base = git(repository, "rev-parse", "dev")
    dependency_file = "dependency work\nfile.txt" if stacked == "dependency-spaced" else "blocker.txt"
    if stacked.startswith("dependency"):
        (root / dependency_file).write_text("dependency")
        git(root, "add", dependency_file)
        git(root, "commit", "-qm", "Dependency work")
        (root / dependency_file).write_text("completed dependency")
        git(root, "commit", "-qam", "Finish dependency")
        base = git(root, "rev-parse", "HEAD")
        git(repository, "merge", "--squash", base)
        git(repository, "commit", "-qm", "Squash dependency")
        if stacked in {"dependency-modified", "dependency-spaced"}:
            (repository / dependency_file).write_text("newer dependency")
            git(repository, "commit", "-qam", "Advance dependency after squash")
    (root / "task.txt").write_text("parked work")
    git(root, "add", "task.txt")
    git(root, "commit", "-qm", "Parked task work")
    for number in range(21):
        (repository / f"upstream{number}.txt").write_text(str(number))
        git(repository, "add", f"upstream{number}.txt")
        git(repository, "commit", "-qm", f"Upstream {number}")
    git(repository, "push", "origin", "dev")
    if stacked != "dependency":
        git(root, "fetch", "origin")
        git(root, "rebase", "--onto", "origin/dev", base)
        (repository / "later.txt").write_text("after manual rebase")
        git(repository, "add", "later.txt")
        git(repository, "commit", "-qm", "Advance dev after manual rebase")
        git(repository, "push", "origin", "dev")
    git(root, "push", "origin", "HEAD:engineer-test")
    p.task.update(parked_on=["dependency"], stacked_base=base)
    p.ledger.state = lambda slug: {"tasks": [p.task, {"id": "dependency", "state": "done"}]}
    counted = []
    native = stack._rebase

    def rebase(start: str) -> None:
        counted.append(git(root, "rev-list", "--count", f"{start}..HEAD"))
        native(start)

    native_shell = stack.shell

    def shell(argv: list[str]) -> subprocess.CompletedProcess[str]:
        failures = {
            "the task shares no history with dev": argv[1:] == ["merge-base", "HEAD", "origin/dev"],
            "cannot resolve the completed rebase tree": argv[1:2] == ["rev-parse"] and argv[-1].endswith("^{tree}"),
            "the stacked base shares no history with dev": argv[1:3] == ["merge-base", base],
            "cannot list dependency files": argv[1:3] == ["diff", "--name-only"],
            "cannot read completed dependency history": argv[1:3] == ["--literal-pathspecs", "rev-list"],
        }
        if failures.get(failure):
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="deliberate failure")
        return native_shell(argv)

    monkeypatch.setattr(stack, "_rebase", rebase)
    monkeypatch.setattr(stack, "shell", shell)
    monkeypatch.chdir(root)

    if failure:
        with pytest.raises(stack.SwarmError) as caught:
            stack.restack("swarm", p.agent, p.ledger)
        assert str(caught.value) == f"{failure}: deliberate failure"
        assert p.task["parked_on"] == ["dependency"]
        assert counted == []
        return

    stack.restack("swarm", p.agent, p.ledger)

    assert counted == [replayed]
    assert git(root, "rev-list", "--count", "origin/dev..HEAD") == replayed
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


@pytest.mark.feature("panic-publish-holds")
def test_publish_under_panic_refuses_an_unregistered_branch_and_pushes_nothing(publication, monkeypatch):
    import fakeredis

    from hooks.context import blocker_window
    from scripts.swarm import panic, publish, store
    from scripts.swarm.store import RedisStore, SwarmError

    fake = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(store, "redis_client", lambda environ=None: fake)
    panic.engage(RedisStore(fake), "swarm", "60 errors in the last hour", False, [], 0)
    with pytest.raises(SwarmError) as refused:
        publish.publish(
            "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
        )
    frozen = blocker_window.FROZEN.format(slug="swarm", reason="60 errors in the last hour")
    assert (
        str(refused.value) == f"{frozen}; Next: agentihooks swarm swarm publish --message 'Change chosen' -- chosen.txt"
    )
    assert git(publication.root, "ls-remote", "origin", "refs/heads/engineer-test") == ""
    assert git(publication.root, "show", "HEAD:chosen.txt") == "after"
    assert not (publication.root / ".git-gate-head").exists()


@pytest.mark.feature("panic-publish-holds")
def test_publish_asks_the_panic_push_check_for_its_worktree_and_pushed_branch(publication, monkeypatch):
    from hooks.context import blocker_window
    from scripts.swarm import publish

    asked = []

    def held_push(cwd, branch, slug):
        asked.append((cwd, branch, slug))

    monkeypatch.setattr(blocker_window, "held_push", held_push)
    result = publish.publish(
        "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
    )
    assert asked == [(str(publication.root), "engineer-test", "swarm")]
    assert git(publication.root, "ls-remote", "origin", "refs/heads/engineer-test").split()[0] == result["head"]


LAB_HOOK = (
    "#!/bin/sh\n"
    "while read old new ref; do\n"
    '  echo "$ref $GIT_PUSH_OPTION_0" >> pushed\n'
    '  echo "lab grade: http://lab.invalid/grades/$new"\n'
    "done\n"
)


@pytest.fixture
def lab(publication: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bare = tmp_path / "lab" / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    git(bare, "config", "receive.advertisePushOptions", "true")
    hook = bare / "hooks" / "post-receive"
    hook.write_text(LAB_HOOK)
    hook.chmod(0o755)
    monkeypatch.setenv("AGENTIHOOKS_LAB_REMOTE", str(bare))
    return bare


def _publish_via_lab(publication: SimpleNamespace, direct: bool = False) -> dict:
    from scripts.swarm import publish

    return publish.publish(
        "swarm",
        publication.agent,
        str(publication.repo),
        publication.ledger,
        "Change chosen",
        ["chosen.txt"],
        publish.LabRoute("t1", direct),
    )


def _origin(publication: SimpleNamespace) -> str:
    return git(publication.root, "ls-remote", "origin", "refs/heads/engineer-test")


@pytest.mark.feature("lab-remote-in-workspaces")
def test_publish_via_lab_grades_head_on_the_lab_before_origin_and_pushes_origin_on_green(
    publication: SimpleNamespace, lab: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    from scripts.swarm import publish

    answers = iter(
        [
            {"grades": []},
            {"grades": [{"conclusion": None}]},
            {"grades": [{"conclusion": "failure"}, {"conclusion": "success"}]},
        ]
    )
    read, slept = [], []

    def read_grade(url: str) -> dict:
        read.append((url, _origin(publication)))
        return next(answers)

    monkeypatch.setattr(publish, "read_grade", read_grade)
    monkeypatch.setattr(publish.time, "sleep", lambda seconds: slept.append(seconds) if seconds >= 1 else None)
    result = _publish_via_lab(publication)
    head = git(publication.root, "rev-parse", "HEAD")
    url = f"http://lab.invalid/grades/{head}"
    assert result["lab_grade"] == url
    assert read == [(url, ""), (url, ""), (url, "")]
    assert slept == [15, 15]
    assert git(lab, "rev-parse", "refs/grade/t1") == head
    assert (lab / "pushed").read_text() == "refs/grade/t1 branch=engineer-test\n"
    assert _origin(publication).split()[0] == head
    assert git(publication.root, "remote", "get-url", "lab") == str(lab)
    shown = capfd.readouterr().err
    assert f"remote: lab grade: {url}" in shown
    assert shown.endswith("\n")


@pytest.mark.feature("lab-remote-in-workspaces")
def test_publish_via_lab_leaves_origin_untouched_on_a_red_grade(
    publication: SimpleNamespace, lab: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm import publish
    from scripts.swarm.store import SwarmError

    monkeypatch.setattr(publish, "read_grade", lambda url: {"grades": [{"conclusion": "failure"}]})
    monkeypatch.setattr(publish, "LAB_WAIT_S", 0)
    with pytest.raises(SwarmError) as refused:
        _publish_via_lab(publication)
    head = git(publication.root, "rev-parse", "HEAD")
    assert str(refused.value).startswith(
        f"the lab grade http://lab.invalid/grades/{head} concluded failure; origin was not pushed; Next: "
    )
    assert git(lab, "rev-parse", "refs/grade/t1") == head
    assert _origin(publication) == ""


@pytest.mark.feature("lab-remote-in-workspaces")
def test_publish_via_lab_direct_pushes_origin_after_the_lab_without_waiting(
    publication: SimpleNamespace, lab: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm import publish

    monkeypatch.setattr(publish, "read_grade", lambda url: pytest.fail("a direct publish read the grade"))
    result = _publish_via_lab(publication, direct=True)
    head = git(publication.root, "rev-parse", "HEAD")
    assert result["lab_grade"] == f"http://lab.invalid/grades/{head}"
    assert git(lab, "rev-parse", "refs/grade/t1") == head
    assert _origin(publication).split()[0] == head


@pytest.mark.feature("swarm-push-wait")
@pytest.mark.parametrize("direct", [False, True])
def test_lab_publication_records_its_head_and_wait_before_polling(
    publication: SimpleNamespace, lab: Path, monkeypatch: pytest.MonkeyPatch, direct: bool
) -> None:
    from typing import Any

    import fakeredis

    from hooks.context import swarm_heartbeat
    from scripts.swarm import cli, idle, pr_wait, publish, waits

    redis = fakeredis.FakeRedis(decode_responses=True)
    store: Any = SimpleNamespace(redis=redis, agents=lambda slug: [publication.agent])
    monkeypatch.setattr(pr_wait, "time", SimpleNamespace(time=lambda: 2.0))
    monkeypatch.setattr(cli, "_worker", lambda store, args: publication.agent)
    monkeypatch.setattr(cli, "_local_repo", lambda store, slug: str(publication.repo))
    monkeypatch.setattr(cli, "LedgerClient", lambda: publication.ledger)

    def recorded() -> None:
        rows = {"t1": {"id": "t1", "pr_url": "https://github.com/o/r/pull/1"}}
        assert (
            pr_wait.enforce_pass(
                store, "swarm", rows, lambda url: pytest.fail("read old origin PR during an active lab wait"), 2001
            )
            == []
        )
        head = git(lab, "rev-parse", "refs/grade/t1")
        marked = pr_wait.read(redis, "swarm", publication.agent.name) or {}
        assert marked["pushed_head"] == head
        assert marked["pushed_at"] == "2000"
        assert marked["pushed_branch"] == "refs/grade/t1"
        assert marked["pushed_repo"] == ""
        held = idle.wait(redis, "swarm", publication.agent.name) or {}
        assert held["on"] == {"kind": "lab", "target": f"http://lab.invalid/grades/{head}"}
        assert held["reason"] == pr_wait.REASON
        assert held["at"] == 2000
        assert held["until"] == 2000 + waits.CHECKED_MINUTES * 60_000

    def grade(url: str) -> dict:
        recorded()
        return {"grades": [{"conclusion": "success"}]}

    monkeypatch.setattr(publish, "read_grade", grade)
    flags = ["--direct"] if direct else []
    args = cli.build_parser().parse_args(
        ["swarm", "publish", "--via", "lab", *flags, "-m", "Change chosen", "chosen.txt"]
    )
    result = cli.publication_command(store, args)
    recorded()
    env = {
        "AGENTIHOOKS_SWARM": "swarm",
        "AGENTIHOOKS_AGENT_NAME": publication.agent.name,
        "AGENTIHOOKS_SWARM_TASK": "t1",
    }
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "agentihooks swarm swarm publish --via lab -m change -- chosen.txt"},
        "cwd": str(publication.repo),
    }
    assert swarm_heartbeat.push_mark(payload, env, redis) is True
    recorded()
    assert result["head"] == git(lab, "rev-parse", "refs/grade/t1")


@pytest.mark.feature("lab-remote-in-workspaces")
def test_publish_via_lab_pushes_the_grade_ref_with_push_negotiation(
    publication: SimpleNamespace, lab: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm import publish

    real = publish.git_https.run
    pushed: list[list[str]] = []

    def run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        if argv[-1].startswith("HEAD:refs/grade/"):
            pushed.append(argv)
        return real(argv, cwd=cwd)

    monkeypatch.setattr(publish.git_https, "run", run)
    _publish_via_lab(publication, direct=True)
    assert pushed == [
        ["git", "-c", "push.negotiate=true", "push", "-o", "branch=engineer-test", "lab", "HEAD:refs/grade/t1"]
    ]
    assert (lab / "pushed").read_text() == "refs/grade/t1 branch=engineer-test\n"


@pytest.mark.feature("lab-remote-in-workspaces")
def test_publish_via_lab_refuses_without_a_lab_remote_for_the_repository(
    publication: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm.store import SwarmError

    monkeypatch.delenv("AGENTIHOOKS_LAB_REMOTE", raising=False)
    with pytest.raises(SwarmError) as refused:
        _publish_via_lab(publication, direct=True)
    assert str(refused.value).startswith(
        "publish --via lab needs AGENTIHOOKS_LAB_REMOTE naming the lab remote of this repository; Next: "
    )
    assert _origin(publication) == ""


@pytest.mark.feature("publish-refusal-keeps-lab-route")
@pytest.mark.parametrize(("direct", "flags"), [(False, ["--via", "lab"]), (True, ["--via", "lab", "--direct"])])
def test_publish_via_lab_refusal_repeats_its_lab_route_in_the_next_command(
    publication: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, direct: bool, flags: list[str]
) -> None:
    import shlex

    from scripts.swarm import cli
    from scripts.swarm.store import SwarmError

    monkeypatch.delenv("AGENTIHOOKS_LAB_REMOTE", raising=False)
    with pytest.raises(SwarmError) as refused:
        _publish_via_lab(publication, direct=direct)
    repeated = str(refused.value).split("; Next: ", 1)[1]
    assert repeated == shlex.join(
        ["agentihooks", "swarm", "swarm", "publish", "--message", "Change chosen", *flags, "--", "chosen.txt"]
    )
    args = cli.build_parser().parse_args(shlex.split(repeated)[2:])
    assert (args.via, args.direct, args.files) == ("lab", direct, ["chosen.txt"])


@pytest.mark.feature("lab-remote-in-workspaces")
@pytest.mark.parametrize(
    ("hook", "code"),
    [("post-receive", "true"), ("pre-receive", 'echo "lab grade: http://lab.invalid/grades/refused"; exit 1')],
)
def test_publish_via_lab_refuses_a_lab_push_without_a_landed_grade(
    publication: SimpleNamespace, lab: Path, hook: str, code: str
) -> None:
    from scripts.swarm.store import SwarmError

    (lab / "hooks" / hook).write_text(f"#!/bin/sh\ncat >/dev/null\n{code}\n")
    (lab / "hooks" / hook).chmod(0o755)
    with pytest.raises(SwarmError) as refused:
        _publish_via_lab(publication, direct=True)
    assert str(refused.value).startswith("the lab push named no grade url; origin was not pushed: ")
    assert _origin(publication) == ""


@pytest.mark.feature("lab-remote-in-workspaces")
def test_lab_conclusion_refuses_once_the_wait_runs_out(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import publish
    from scripts.swarm.store import SwarmError

    clock, slept = iter([0, 1799, 1800]), []
    monkeypatch.setattr(publish.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(publish.time, "sleep", slept.append)
    monkeypatch.setattr(publish, "read_grade", lambda url: {"grades": [{"conclusion": None}]})
    with pytest.raises(SwarmError) as refused:
        publish.lab_conclusion("http://lab.invalid/grades/abc")
    assert str(refused.value) == (
        "the lab grade http://lab.invalid/grades/abc has not concluded after 1800 s; origin was not pushed"
    )
    assert slept == [15]


@pytest.mark.feature("lab-remote-in-workspaces")
@pytest.mark.parametrize(
    "answer",
    [urllib.error.URLError("refused"), json.JSONDecodeError("no json", "", 0), {"head": "abc"}],
)
def test_an_unreadable_lab_grade_refuses_the_wait(monkeypatch: pytest.MonkeyPatch, answer: object) -> None:
    from scripts.swarm import publish
    from scripts.swarm.store import SwarmError

    def read_grade(url: str) -> object:
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(publish, "read_grade", read_grade)
    with pytest.raises(SwarmError) as refused:
        publish.lab_conclusion("http://lab.invalid/grades/abc")
    cause = answer if isinstance(answer, Exception) else KeyError("grades")
    assert str(refused.value) == (
        f"the lab grade http://lab.invalid/grades/abc could not be read, so origin was not pushed: {cause!r}"
    )


@pytest.mark.feature("lab-remote-in-workspaces")
def test_publish_direct_without_the_lab_route_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    import syslog
    from typing import Any

    from scripts.swarm import cli, publish
    from scripts.swarm.store import SwarmError

    store: Any = None
    monkeypatch.setattr(syslog, "syslog", lambda *args: None)
    monkeypatch.setattr(cli, "_worker", lambda store, args: SimpleNamespace(task="t1", name="engineer@test"))
    monkeypatch.setattr(cli, "_local_repo", lambda store, slug: "/repo")
    monkeypatch.setattr(cli, "LedgerClient", lambda: SimpleNamespace(task=lambda slug, task: {}))
    monkeypatch.setattr(publish, "refusal_branch", lambda repo, task: "")
    monkeypatch.setattr(publish, "publish", lambda *args: pytest.fail("published without the lab route"))
    args = cli.build_parser().parse_args(["swarm", "publish", "--direct", "-m", "Change", "chosen.txt"])
    with pytest.raises(SwarmError) as refused:
        cli.publication_command(store, args)
    assert str(refused.value) == "publish --direct goes with --via lab"


@pytest.mark.feature("lab-remote-in-workspaces")
def test_read_grade_reads_the_lab_answer_with_its_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    from scripts.swarm import publish

    opened = []

    def urlopen(url: str, timeout: int) -> io.BytesIO:
        opened.append((url, timeout))
        return io.BytesIO(b'{"head": "abc", "grades": []}')

    monkeypatch.setattr(publish.urllib.request, "urlopen", urlopen)
    assert publish.read_grade("http://lab.invalid/grades/abc") == {"head": "abc", "grades": []}
    assert opened == [("http://lab.invalid/grades/abc", 10)]


@pytest.mark.feature("lab-remote-in-workspaces")
@pytest.mark.parametrize(
    ("flags", "route"),
    [([], None), (["--via", "lab"], ("t1", False)), (["--via", "lab", "--direct"], ("t1", True))],
)
def test_publish_command_hands_the_lab_route_to_publish(
    monkeypatch: pytest.MonkeyPatch, flags: list[str], route: tuple[str, bool] | None
) -> None:
    from typing import Any

    from scripts.swarm import cli, publish

    called = []
    store: Any = SimpleNamespace(redis=object())
    monkeypatch.setattr(cli, "_worker", lambda store, args: SimpleNamespace(task="t1", name="engineer@test"))
    monkeypatch.setattr(cli, "_local_repo", lambda store, slug: "/repo")
    monkeypatch.setattr(cli, "LedgerClient", lambda: SimpleNamespace(task=lambda slug, task: {}))
    monkeypatch.setattr(publish, "publish", lambda *args: called.append(args) or {})
    args = cli.build_parser().parse_args(["swarm", "publish", *flags, "-m", "Change", "chosen.txt"])
    cli.publication_command(store, args)
    assert called[0][5] == ["chosen.txt"]
    assert called[0][6] == (publish.LabRoute(*route) if route else None)


@pytest.mark.feature("lab-remote-in-workspaces")
def test_publish_command_names_its_lab_flags(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import re

    from scripts.swarm import cli

    monkeypatch.setenv("COLUMNS", "200")
    parser = cli.build_parser()
    assert parser.parse_args(["swarm", "publish", "-m", "Change"]).via == ""
    with pytest.raises(SystemExit) as refused:
        parser.parse_args(["swarm", "publish", "--via", "origin", "-m", "Change"])
    assert refused.value.code == 2
    with pytest.raises(SystemExit):
        parser.parse_args(["swarm", "publish", "--help"])
    shown = capsys.readouterr().out
    assert re.search(r"^  --via \{lab\} +grade HEAD on the lab remote before origin$", shown, re.M)
    assert re.search(r"^  --direct +with --via lab, push origin without the lab grade$", shown, re.M)


NOTICE_SIGNER = "notice-signer"
NOTICE_ACTOR = "agenticore-app[bot]"


@pytest.fixture
def lab_receiver(publication: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """Serve the real webhook receiver with lab intake on a local port and point the notice settings at it.

    The task worktree fetches from a GitHub origin url and pushes to its local remote, and tips keeps the remote task
    branch as the receiver saw it at each notice it accepted."""
    from scripts.ci_lab.head_notice import ACTOR_ENV, URL_ENV
    from scripts.ci_lab.webhooks import NOTICE_SECRET_ENV, LabIntake
    from scripts.events.config import BusConfig
    from scripts.events.envelope import Envelope
    from scripts.events.memory import open_bus
    from scripts.hub import github_webhooks

    remote = publication.repo.parent / "remote.git"
    git(publication.root, "remote", "set-url", "origin", "git@github.com:Acme/Widgets.git")
    git(publication.root, "config", "remote.origin.pushurl", str(remote))
    bus = open_bus(BusConfig())
    heads = bus.lab_heads.consumer("lab", "ci.lab.head.>")
    tips: list[str] = []

    class Witness:
        def publish(self, subject: str, event: Envelope) -> bool:
            tips.append(git(publication.root, "ls-remote", str(remote), "refs/heads/engineer-test"))
            return bus.lab_heads.publish(subject, event)

    intake = LabIntake(Witness(), NOTICE_SIGNER.encode())
    pulls = github_webhooks.LazyPulls(bus)  # pyright: ignore[reportArgumentType]
    ingress = github_webhooks.Ingress(pulls, b"webhook-signer", intake)
    server = ThreadingHTTPServer(("localhost", 0), github_webhooks.handler(ingress))
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    monkeypatch.setenv(URL_ENV, f"http://localhost:{server.server_address[1]}{github_webhooks.NOTICE_PATH}")
    monkeypatch.setenv(NOTICE_SECRET_ENV, NOTICE_SIGNER)
    monkeypatch.setenv(ACTOR_ENV, NOTICE_ACTOR)
    yield SimpleNamespace(heads=heads, remote=remote, tips=tips)
    server.shutdown()
    server.server_close()
    serving.join()


@pytest.mark.feature("lab-head-notice-push")
def test_a_published_push_sends_one_signed_notice_the_receiver_accepts(
    publication: SimpleNamespace, lab_receiver: SimpleNamespace
) -> None:
    from scripts.swarm import publish

    result = publish.publish(
        "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
    )
    head = git(publication.root, "rev-parse", "HEAD")
    notice = {
        "event": "notice",
        "repo": "https://github.com/acme/widgets",
        "head": head,
        "base": git(publication.root, "merge-base", head, "origin/dev"),
        "actor": NOTICE_ACTOR,
        "action": "pushed",
    }
    deliveries = lab_receiver.heads.fetch(100, 0)
    assert [(delivery.event.type, delivery.event.data) for delivery in deliveries] == [("ci.lab.head.notice", notice)]
    assert lab_receiver.tips == [f"{head}\trefs/heads/engineer-test"]
    assert result["lab_notice"] == "200"


def refuse_the_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sign the notice with a secret the receiver does not hold, so it answers 401."""
    from scripts.ci_lab.webhooks import NOTICE_SECRET_ENV

    monkeypatch.setenv(NOTICE_SECRET_ENV, "another-signer")


def send_the_notice_nowhere(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the notice at a local port nothing listens on, so the connection is refused."""
    import socket

    from scripts.ci_lab.head_notice import URL_ENV

    with socket.socket() as probe:
        probe.bind(("localhost", 0))
        port = probe.getsockname()[1]
    monkeypatch.setenv(URL_ENV, f"http://localhost:{port}/lab/head")


@pytest.mark.feature("lab-head-notice-push")
@pytest.mark.parametrize("failure", [refuse_the_notice, send_the_notice_nowhere], ids=["refused", "unreachable"])
def test_a_failed_notice_never_fails_the_push(
    publication: SimpleNamespace,
    lab_receiver: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: Callable[[pytest.MonkeyPatch], None],
) -> None:
    from scripts.swarm import publish

    failure(monkeypatch)
    result = publish.publish(
        "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
    )
    head = git(publication.root, "rev-parse", "HEAD")
    prefix, suffix = f"lab head notice for {head} was not accepted: ", "; the push stands"
    lines = capsys.readouterr().err.splitlines()
    assert any(line.startswith(prefix) and line.endswith(suffix) for line in lines)
    assert result["lab_notice"] == "0"
    assert lab_receiver.heads.fetch(100, 0) == []
    assert lab_receiver.tips == []
    assert git(publication.root, "ls-remote", str(lab_receiver.remote), "refs/heads/engineer-test").split()[0] == head


@pytest.mark.feature("lab-head-notice-push")
def test_a_publish_without_the_lab_notice_settings_sends_and_records_no_notice(
    publication: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.ci_lab.head_notice import ACTOR_ENV, URL_ENV
    from scripts.ci_lab.webhooks import NOTICE_SECRET_ENV
    from scripts.swarm import publish

    monkeypatch.delenv(URL_ENV, raising=False)
    monkeypatch.delenv(NOTICE_SECRET_ENV, raising=False)
    monkeypatch.delenv(ACTOR_ENV, raising=False)
    result = publish.publish(
        "swarm", publication.agent, str(publication.repo), publication.ledger, "Change chosen", ["chosen.txt"]
    )
    assert "lab_notice" not in result
    assert result["head"] == git(publication.root, "rev-parse", "HEAD")


@pytest.mark.feature("lab-head-notice-push")
@pytest.mark.parametrize(
    "origin",
    [
        "git@github.com:The-Cloud-Clockwork/agentihooks.git",
        "ssh://git@github.com/The-Cloud-Clockwork/agentihooks.git",
        "https://github.com/the-cloud-clockwork/agentihooks/",
    ],
    ids=["scp ssh", "ssh url", "https"],
)
def test_the_notice_names_the_repository_as_github_deliveries_do(origin: str) -> None:
    from scripts.swarm import publish

    assert publish.lab_repository(origin) == "https://github.com/the-cloud-clockwork/agentihooks"
