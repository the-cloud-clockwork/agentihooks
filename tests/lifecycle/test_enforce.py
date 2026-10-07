import gzip
import os
import subprocess

import pytest

from hooks.lifecycle import act
from hooks.lifecycle.act import ActionError, Journal, archive_file, finish_pending, remove_worktree, snapshot
from hooks.lifecycle.lease import add_holder
from hooks.lifecycle.model import Holder, Root
from hooks.lifecycle.run import sweep
from hooks.lifecycle.scratch_rm import refusal, remove_scratch

from .conftest import age, git, process, snap

HOUR = 3600


def remote_refs(repo):
    return git(repo.origin, "for-each-ref", "--format=%(refname)", "refs/heads/wip")


def test_snapshot_pushes_wip_with_skip_ci_and_skips_big_files(repo, monkeypatch):
    monkeypatch.setattr(act, "SNAPSHOT_FILE_LIMIT", 10)
    path = repo.worktree("dirty")
    (path / "notes.txt").write_text("small\n")
    (path / "big.bin").write_bytes(b"x" * 100)
    (path / "README").write_text("changed\n")
    ref = snapshot(path)
    assert ref.startswith("wip/primary/dirty-")
    body = git(repo.origin, "log", "-1", "--format=%B", f"refs/heads/{ref}")
    assert "[skip ci]" in body and "skipped over 50 MB: big.bin" in body
    files = git(repo.origin, "ls-tree", "-r", "--name-only", f"refs/heads/{ref}").split()
    assert "notes.txt" in files and "big.bin" not in files
    assert git(repo.origin, "show", f"refs/heads/{ref}:README") == "changed"
    assert git(path, "status", "--porcelain")


def test_remove_worktree_drops_branch_only_when_on_a_remote(repo):
    merged = repo.worktree("merged")
    local = repo.worktree("local")
    (local / "a.txt").write_text("a\n")
    git(local, "add", "a.txt")
    git(local, "commit", "-q", "-m", "local only")
    remove_worktree(merged)
    remove_worktree(local)
    assert not merged.exists() and not local.exists()
    branches = git(repo.primary, "branch", "--format=%(refname:short)").split()
    assert "merged" not in branches and "local" in branches
    assert "merged" not in git(repo.primary, "worktree", "list")


def run_twice(repo, home, fresh_uptime=14_000, **kwargs):
    roots = [repo.root()]
    first = sweep(roots, snap(uptime=10_000), home, act=True, fresh=lambda: snap(uptime=10_000))
    second = sweep(
        roots, snap(uptime=fresh_uptime, **kwargs), home, act=True, fresh=lambda: snap(uptime=fresh_uptime, **kwargs)
    )
    return first, second


def outcomes(report):
    return {os.path.basename(item["path"]): item["outcome"] for item in report["findings"]}


def test_enforce_acts_only_on_the_second_agreeing_sweep(repo, tmp_path):
    clean = repo.worktree("clean")
    dirty = repo.worktree("dirty")
    (dirty / "work.txt").write_text("unsaved\n")
    age(clean, 3 * HOUR)
    age(dirty, 25 * HOUR)
    first, second = run_twice(repo, tmp_path / "state")
    assert outcomes(first) == {"clean": "", "dirty": ""}
    assert [item["due"] for item in first["findings"]] == [False, False]
    result = outcomes(second)
    assert result["clean"] == "removed"
    assert result["dirty"].startswith("pushed wip/primary/dirty-") and result["dirty"].endswith("removed")
    assert not clean.exists() and not dirty.exists()
    assert "refs/heads/wip/primary/dirty-" in remote_refs(repo)


def test_failed_push_keeps_the_worktree(repo, tmp_path):
    dirty = repo.worktree("dirty")
    (dirty / "work.txt").write_text("unsaved\n")
    age(dirty, 25 * HOUR)
    repo.origin.rename(repo.origin.with_name("moved.git"))
    _first, second = run_twice(repo, tmp_path / "state")
    assert outcomes(second)["dirty"].startswith("failed: git push")
    assert (dirty / "work.txt").exists()


def test_enforce_rechecks_safety_right_before_acting(repo, tmp_path):
    clean = repo.worktree("clean")
    age(clean, 3 * HOUR)
    roots = [repo.root()]
    home = tmp_path / "state"
    sweep(roots, snap(uptime=10_000), home, act=True, fresh=lambda: snap(uptime=10_000))
    late = lambda: snap(uptime=14_000, cwds=(str(clean),))  # noqa: E731
    report = sweep(roots, snap(uptime=14_000), home, act=True, fresh=late)
    assert outcomes(report)["clean"] == "skipped: no longer safe"
    assert clean.exists()


def test_journal_finishes_an_interrupted_removal(repo, tmp_path):
    journal = Journal(tmp_path / "gc-journal.json")
    half = repo.worktree("half")
    task = tmp_path / "scratch" / "repo" / "task"
    task.mkdir(parents=True)
    journal.begin(str(half), "worktree")
    journal.begin(str(task), "scratch")
    assert sorted(finish_pending(journal)) == sorted([str(half), str(task)])
    assert not half.exists() and not task.exists()
    assert journal.pending() == {}
    assert "half" not in git(repo.primary, "worktree", "list")


def test_archive_gzips_and_removes_the_original(tmp_path):
    source = tmp_path / "chat-1.txt"
    source.write_text("hello\n")
    archive_file(source)
    assert not source.exists()
    assert gzip.decompress((tmp_path / "archive" / "chat-1.txt.gz").read_bytes()) == b"hello\n"


def scratch_root(tmp_path):
    return [Root("scratchpad", str(tmp_path / "scratch"), "scratch")]


def test_scratch_rm_refusals(repo, tmp_path):
    roots = scratch_root(tmp_path)
    task = tmp_path / "scratch" / "repo" / "task"
    task.mkdir(parents=True)
    assert refusal(tmp_path / "scratch" / "repo", roots, snap(), 1).startswith("not a task dir")
    assert refusal(tmp_path / "elsewhere" / "x" / "y", roots, snap(), 1).startswith("not a task dir")
    proc = tmp_path / "proc"
    (proc / "700").mkdir(parents=True)
    os.symlink(task, proc / "700" / "cwd")
    view = snap(table={700: process(700, comm="bash"), 800: process(800, start=8)})
    assert refusal(task, roots, view, caller=1, proc=proc) == "process 700 (bash) works inside"
    assert refusal(task, roots, view, caller=700, proc=proc) == ""
    add_holder(task, "scratch", Holder("", 800, 8, "boot-1"), 0.0)
    assert refusal(task, roots, view, caller=700, proc=proc) == "held by another live session"
    nested = repo.worktree("wt", base=tmp_path / "scratch" / "repo" / "dirty-task")
    (nested / "work.txt").write_text("unsaved\n")
    assert "holds uncommitted or unpushed work" in refusal(nested.parent, roots, snap(), 1)


def test_scratch_rm_removes_nested_worktrees_through_git(repo, tmp_path):
    roots = scratch_root(tmp_path)
    nested = repo.worktree("wt", base=tmp_path / "scratch" / "repo" / "task")
    remove_scratch(nested.parent, roots, snap(), 1)
    assert not nested.parent.exists()
    assert "wt" not in git(repo.primary, "worktree", "list")
    with pytest.raises(ActionError):
        remove_scratch(tmp_path / "scratch" / "repo", roots, snap(), 1)


def test_scratch_rm_skips_empty_git_marker_and_still_refuses_real_worktree(repo, tmp_path):
    roots = scratch_root(tmp_path)
    task = tmp_path / "scratch" / "repo" / "uv-task"
    marker = task / ".cache" / "uv" / "git-v0" / "checkouts"
    marker.mkdir(parents=True)
    (marker / ".git").write_text("")
    assert refusal(task, roots, snap(), 1) == ""
    remove_scratch(task, roots, snap(), 1)
    assert not task.exists()
    nested = repo.worktree("wt", base=tmp_path / "scratch" / "repo" / "real-task")
    (nested / "work.txt").write_text("unsaved\n")
    with pytest.raises(ActionError, match="holds uncommitted or unpushed work"):
        remove_scratch(nested.parent, roots, snap(), 1)
    assert nested.exists()


unreadable_needs_a_user = pytest.mark.skipif(os.geteuid() == 0, reason="root reads every folder")


def deny(request, path, mode):
    os.chmod(path, mode)
    request.addfinalizer(lambda: os.chmod(path, 0o755))


def idle_tasks(tmp_path, *names):
    tasks = [tmp_path / "scratch" / name for name in names]
    for task in tasks:
        task.mkdir(parents=True)
    for task in tasks:
        age(task, 30 * 24 * HOUR)
    return tasks


def sweeps(tmp_path, count):
    roots, home = scratch_root(tmp_path), tmp_path / "state"
    return [sweep(roots, snap(uptime=10_000 + 4 * HOUR * n), home, act=True, fresh=snap) for n in range(count)]


def findings_by_name(report):
    return {os.path.basename(item["path"]): item for item in report["findings"]}


@unreadable_needs_a_user
def test_enforce_skips_folders_it_cannot_read_and_sweeps_the_rest(request, tmp_path):
    locked_group, locked_task, plain = idle_tasks(tmp_path, "locked/task", "repo/locked-task", "repo/plain-task")
    deny(request, locked_group.parent, 0)
    deny(request, locked_task, 0)
    second = findings_by_name(sweeps(tmp_path, 2)[1])
    for name in ("locked", "locked-task"):
        assert second[name]["action"] == "keep"
        assert second[name]["reason"] == "unreadable, skipped: Permission denied"
    assert second["plain-task"]["outcome"] == "removed"
    assert not plain.exists() and locked_task.exists()


@unreadable_needs_a_user
def test_failed_removal_names_the_folder_and_later_sweeps_still_run(request, tmp_path):
    site_task, plain = idle_tasks(tmp_path, "repo/site-task", "repo/plain-task")
    site = site_task / "venv" / "site"
    site.mkdir(parents=True)
    (site / "mod.py").write_text("x\n")
    age(site_task, 30 * 24 * HOUR)
    deny(request, site, 0o555)
    reports = sweeps(tmp_path, 3)
    for report in reports[1:]:
        outcome = findings_by_name(report)["site-task"]["outcome"]
        assert outcome.startswith("failed: [Errno 13] Permission denied") and str(site) in outcome
    assert findings_by_name(reports[1])["plain-task"]["outcome"] == "removed"
    assert not plain.exists() and (site / "mod.py").exists()


@unreadable_needs_a_user
def test_journal_drops_a_removal_it_cannot_finish_and_finishes_the_rest(request, tmp_path):
    journal = Journal(tmp_path / "gc-journal.json")
    stuck, plain = idle_tasks(tmp_path, "repo/stuck", "repo/plain")
    (stuck / "site").mkdir()
    (stuck / "site" / "mod.py").write_text("x\n")
    deny(request, stuck / "site", 0o555)
    journal.begin(str(stuck), "scratch")
    journal.begin(str(plain), "scratch")
    assert finish_pending(journal) == [str(plain)]
    assert journal.pending() == {}
    assert stuck.exists() and not plain.exists()


@pytest.mark.parametrize("error", [ActionError("git worktree"), subprocess.TimeoutExpired("git", 300)])
def test_journal_drops_a_removal_that_git_fails_or_times_out(monkeypatch, tmp_path, error):
    journal = Journal(tmp_path / "gc-journal.json")
    (stuck,) = idle_tasks(tmp_path, "repo/stuck")

    def fail(path):
        raise error

    monkeypatch.setattr(act, "remove_path", fail)
    journal.begin(str(stuck), "scratch")
    assert finish_pending(journal) == []
    assert journal.pending() == {}
