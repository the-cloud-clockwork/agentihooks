from hooks.lifecycle.lease import (
    MAX_HOLDERS,
    SCRATCH_LEASE,
    WORKTREE_LEASE,
    add_holder,
    admin_dir,
    lease_path,
    read_lease,
)
from hooks.lifecycle.model import Holder


def test_admin_dir_reads_linked_worktree_and_ignores_primary(repo):
    path = repo.worktree("task")
    assert admin_dir(path) == repo.primary / ".git" / "worktrees" / "task"
    assert admin_dir(repo.primary) is None
    assert lease_path(path, "worktree") == repo.primary / ".git" / "worktrees" / "task" / WORKTREE_LEASE


def test_add_holder_dedupes_and_caps(tmp_path):
    work = tmp_path / "scratch-task"
    work.mkdir()
    add_holder(work, "scratch", Holder("s-1", 1, 1, "b"), 10.0)
    add_holder(work, "scratch", Holder("s-1", 2, 2, "b"), 20.0)
    lease = read_lease(work, "scratch")
    assert (work / SCRATCH_LEASE).exists()
    assert lease.created_at == 10.0
    assert lease.holders == (Holder("s-1", 2, 2, "b"),)
    for pid in range(20):
        add_holder(work, "scratch", Holder("", pid, pid, "b"), 30.0)
    assert len(read_lease(work, "scratch").holders) == MAX_HOLDERS


def test_unreadable_or_missing_lease_is_none(tmp_path):
    assert read_lease(tmp_path, "scratch") is None
    (tmp_path / SCRATCH_LEASE).write_text("{not json")
    assert read_lease(tmp_path, "scratch") is None
    assert read_lease(tmp_path, "worktree") is None
