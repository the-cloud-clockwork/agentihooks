from hooks.lifecycle.lease import add_holder, admin_dir
from hooks.lifecycle.model import Holder, Root
from hooks.lifecycle.worktrees import classify, discover, nested_worktrees

from .conftest import age, git, process, snap

HOUR = 3600


def verdict(repo, path, **kwargs):
    finding = classify(path, repo.root(), snap(**kwargs))
    return finding.action, finding.reason


def test_clean_pushed_idle_is_removed_and_recent_is_kept(repo):
    path = repo.worktree("done-task")
    age(path, 3 * HOUR)
    assert verdict(repo, path) == ("remove", "clean and on a remote")
    age(path, 600)
    assert verdict(repo, path) == ("keep", "clean, recent")


def test_dirty_worktree_waits_a_day_then_snapshots(repo):
    path = repo.worktree("dirty-task")
    (path / "notes.txt").write_text("unsaved\n")
    age(path, 3 * HOUR)
    assert verdict(repo, path) == ("keep", "unique work, recent")
    age(path, 25 * HOUR)
    assert verdict(repo, path)[0] == "snapshot"


def test_unpushed_commit_counts_as_unique_work(repo):
    path = repo.worktree("local-commit")
    (path / "a.txt").write_text("a\n")
    git(path, "add", "a.txt")
    git(path, "commit", "-q", "-m", "local")
    age(path, 25 * HOUR)
    assert verdict(repo, path)[0] == "snapshot"


def test_protected_busy_and_in_use_are_never_actionable(repo):
    main = repo.worktree("main")
    age(main, 30 * HOUR)
    assert verdict(repo, main) == ("keep", "protected branch main")
    busy = repo.worktree("busy")
    age(busy, 30 * HOUR)
    (admin_dir(busy) / "MERGE_HEAD").write_text("x\n")
    assert verdict(repo, busy) == ("skip", "git MERGE_HEAD in progress")
    used = repo.worktree("used")
    age(used, 30 * HOUR)
    assert verdict(repo, used, cwds=(str(used / "src"),)) == ("keep", "a process works inside")
    assert verdict(repo, used, cwds=(str(used) + "-other",)) == ("remove", "clean and on a remote")


def test_live_owner_and_boot_grace_hold_the_worktree(repo):
    path = repo.worktree("owned")
    add_holder(path, "worktree", Holder("", 4242, 77, "boot-1"), 0.0)
    age(path, 30 * HOUR)
    assert verdict(repo, path, table={4242: process(4242, start=77)}) == ("keep", "owner session alive")
    assert verdict(repo, path, table={4242: process(4242, start=78)}) == ("remove", "clean and on a remote")
    assert verdict(repo, path, uptime=600) == ("keep", "boot grace")


def test_resumed_session_keeps_its_worktree(repo):
    path = repo.worktree("resumed")
    add_holder(path, "worktree", Holder("sess-1", 100, 5, "old-boot"), 0.0)
    age(path, 30 * HOUR)
    assert verdict(repo, path, sessions={900: "sess-1"}) == ("keep", "owner session alive")
    assert verdict(repo, path, sessions={900: "sess-2"})[0] == "remove"


def test_ephemeral_worktree_is_removed_without_rescue(repo):
    path = repo.worktree("plant")
    (path / "planted.txt").write_text("fault\n")
    add_holder(path, "ephemeral", Holder("", 1, 1, "gone"), 0.0)
    age(path, 3 * HOUR)
    assert verdict(repo, path) == ("remove", "throwaway checkout, owner gone")


def test_discover_finds_tmp_and_nested_scratch_worktrees(repo, tmp_path):
    top = repo.worktree("top")
    tmp = repo.worktree("x-1", base=repo.trees / "primary" / "_tmp")
    nested = repo.worktree("wt", base=tmp_path / "scratch" / "proj" / "task-1" / "deep")
    roots = [repo.root(), Root("scratchpad", str(tmp_path / "scratch"), "scratch")]
    found = {path for path, _root in discover(roots)}
    assert {top, tmp, nested} <= found
    assert repo.primary not in found


def test_nested_worktrees_skips_plain_dirs_and_empty_git_markers(repo, tmp_path):
    task = tmp_path / "scratch" / "proj" / "task-1"
    marker = task / ".cache" / "uv" / "checkout"
    marker.mkdir(parents=True)
    (marker / ".git").write_text("")
    (task / "notes").mkdir()
    real = repo.worktree("wt", base=task / "deep")
    assert nested_worktrees(task) == [real]


def test_discover_follows_git_registry_to_deeply_nested_worktrees(repo, tmp_path):
    repo.worktree("anchor")
    deep_base = tmp_path / "scratch" / "proj" / "crew" / "notes" / "member" / "task-x" / "a" / "b" / "c"
    deep = repo.worktree("wt-deep", base=deep_base)
    roots = [repo.root(), Root("scratchpad", str(tmp_path / "scratch"), "scratch")]
    found = dict(discover(roots))
    assert found[deep].id == "scratchpad"
    outside = repo.worktree("elsewhere", base=tmp_path / "unmanaged")
    assert outside not in dict(discover(roots))
