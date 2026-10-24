import json
import time

from hooks.lifecycle.gitstate import TreeState
from hooks.lifecycle.lease import add_holder, admin_dir, lease_path
from hooks.lifecycle.liveness import Snapshot
from hooks.lifecycle.model import Finding, Holder, Lease, Root
from hooks.lifecycle.worktrees import CLEAN_IDLE, _idle_verdict, classify, discover, nested_worktrees, stale_after

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
    assert verdict(repo, path, table={4242: process(4242, start=78)}) == ("remove", "clean and on a remote, owner left")
    assert verdict(repo, path, uptime=600) == ("keep", "boot grace")


def test_resumed_session_keeps_its_worktree(repo):
    path = repo.worktree("resumed")
    add_holder(path, "worktree", Holder("sess-1", 100, 5, "old-boot"), 0.0)
    age(path, 30 * HOUR)
    assert verdict(repo, path, sessions={900: "sess-1"}) == ("keep", "owner session alive")
    assert verdict(repo, path, sessions={900: "sess-2"})[0] == "remove"


def test_departed_owner_frees_clean_pushed_worktree_at_once(repo):
    path = repo.worktree("left")
    add_holder(path, "worktree", Holder("sess-gone", 4242, 77, "boot-1"), 0.0)
    age(path, 60)
    finding = classify(path, repo.root(), snap())
    touched = finding.last_touch
    assert finding == Finding(
        str(path), "worktrees", "worktree", "remove", "clean and on a remote, owner left", touched, immediate=True
    )
    assert abs(touched - (time.time() - 60)) < 30
    live = classify(path, repo.root(), snap(table={4242: process(4242, start=77)}))
    assert (live.action, live.reason, live.immediate) == ("keep", "owner session alive", False)
    (path / "notes.txt").write_text("unsaved\n")
    age(path, 60)
    dirty = classify(path, repo.root(), snap())
    assert (dirty.action, dirty.reason, dirty.immediate) == ("keep", "unique work, recent", False)


def test_departed_owner_unpushed_commit_stays_kept(repo):
    path = repo.worktree("left-local")
    add_holder(path, "worktree", Holder("sess-gone", 4242, 77, "boot-1"), 0.0)
    (path / "a.txt").write_text("a\n")
    git(path, "add", "a.txt")
    git(path, "commit", "-q", "-m", "local")
    age(path, 60)
    assert verdict(repo, path) == ("keep", "unique work, recent")


def test_clean_pushed_without_owner_still_waits_for_idle(repo):
    path = repo.worktree("unowned")
    lease_path(path, "worktree").write_text(json.dumps({"kind": "worktree", "created_at": 0.0, "holders": []}))
    age(path, 60)
    finding = classify(path, repo.root(), snap())
    assert (finding.action, finding.reason, finding.immediate) == ("keep", "clean, recent", False)
    age(path, 3 * HOUR)
    idle = classify(path, repo.root(), snap())
    assert (idle.action, idle.reason, idle.immediate) == ("remove", "clean and on a remote", False)


def test_throwaway_and_snapshot_verdicts_are_never_immediate(repo):
    def full(path):
        finding = classify(path, repo.root(), snap())
        return finding.action, finding.reason, finding.immediate

    plant = repo.worktree("plant-x")
    add_holder(plant, "ephemeral", Holder("sess-gone", 1, 1, "gone"), 0.0)
    age(plant, 60)
    assert full(plant) == ("keep", "throwaway, recent", False)
    age(plant, 3 * HOUR)
    assert full(plant) == ("remove", "throwaway checkout, owner gone", False)
    dirty = repo.worktree("dirty-x")
    (dirty / "notes.txt").write_text("unsaved\n")
    age(dirty, 25 * HOUR)
    assert full(dirty) == ("snapshot", "unique work, owner gone: push to wip/ then remove", False)


def test_clean_idle_boundary_is_inclusive():
    clean = TreeState("/w", "/a", "/p", "task", (), False, "", False, 0.0)
    plant = Lease("ephemeral", 0.0)
    assert _idle_verdict(clean, plant, CLEAN_IDLE) == ("remove", "throwaway checkout, owner gone", False)
    assert _idle_verdict(clean, None, CLEAN_IDLE) == ("remove", "clean and on a remote", False)


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


DAY = 24 * HOUR


def test_stale_worktree_is_acted_on_whatever_holds_it(repo, monkeypatch):
    monkeypatch.setenv("WT_STALE_DAYS", "3")
    used = repo.worktree("stale-used")
    age(used, 4 * DAY)
    assert verdict(repo, used, cwds=(str(used),)) == ("remove", "untouched over 3 days though a process works inside")
    assert verdict(repo, used, uptime=600) == ("remove", "untouched over 3 days though boot grace")
    busy = repo.worktree("stale-busy")
    (admin_dir(busy) / "MERGE_HEAD").write_text("x\n")
    age(busy, 4 * DAY)
    assert verdict(repo, busy) == ("remove", "untouched over 3 days though git MERGE_HEAD in progress")
    locked = repo.worktree("stale-locked")
    (admin_dir(locked) / "locked").write_text("")
    age(locked, 4 * DAY)
    assert verdict(repo, locked) == ("remove", "untouched over 3 days though git-locked")
    dirty = repo.worktree("stale-dirty")
    (dirty / "notes.txt").write_text("unsaved\n")
    age(dirty, 4 * DAY)
    assert verdict(repo, dirty, cwds=(str(dirty),)) == (
        "snapshot",
        "untouched over 3 days though a process works inside",
    )
    plant = repo.worktree("stale-plant")
    add_holder(plant, "ephemeral", Holder("", 1, 1, "gone"), 0.0)
    (plant / "notes.txt").write_text("unsaved\n")
    age(plant, 4 * DAY)
    assert verdict(repo, plant, cwds=(str(plant),)) == (
        "remove",
        "untouched over 3 days though a process works inside",
    )
    age(used, 2 * DAY)
    assert verdict(repo, used, cwds=(str(used),)) == ("keep", "a process works inside")


def test_stale_never_overrides_a_protected_branch_or_a_live_owner(repo, monkeypatch):
    monkeypatch.setenv("WT_STALE_DAYS", "1")
    main = repo.worktree("main")
    age(main, 5 * DAY)
    assert verdict(repo, main) == ("keep", "protected branch main")
    owned = repo.worktree("stale-owned")
    add_holder(owned, "worktree", Holder("", 4242, 77, "boot-1"), 0.0)
    age(owned, 5 * DAY)
    assert verdict(repo, owned, table={4242: process(4242, start=77)}) == ("keep", "owner session alive")


def test_stale_boundary_is_inclusive(repo, monkeypatch):
    monkeypatch.setenv("WT_STALE_DAYS", "3")
    used = repo.worktree("edge-used")
    age(used, DAY)
    touched = classify(used, repo.root(), snap()).last_touch
    exact = Snapshot("boot-1", 99999.0, touched + 3 * DAY, {}, (str(used),), {})
    finding = classify(used, repo.root(), exact)
    assert (finding.action, finding.reason) == ("remove", "untouched over 3 days though a process works inside")


def test_a_locked_worktree_mid_merge_is_kept_not_skipped(repo):
    path = repo.worktree("locked-merge")
    (admin_dir(path) / "locked").write_text("")
    (admin_dir(path) / "MERGE_HEAD").write_text("x\n")
    age(path, 30 * HOUR)
    assert verdict(repo, path) == ("keep", "git-locked")


def test_stale_days_default_to_seven_and_are_read_on_every_call(repo, monkeypatch):
    monkeypatch.delenv("WT_STALE_DAYS", raising=False)
    assert stale_after() == 7 * DAY
    used = repo.worktree("week-used")
    age(used, 6 * DAY)
    assert verdict(repo, used, cwds=(str(used),)) == ("keep", "a process works inside")
    age(used, 7 * DAY)
    assert verdict(repo, used, cwds=(str(used),)) == ("remove", "untouched over 7 days though a process works inside")
    monkeypatch.setenv("WT_STALE_DAYS", "0.5")
    assert stale_after() == DAY / 2
    for unusable in ("soon", "0", "-2", "nan"):
        monkeypatch.setenv("WT_STALE_DAYS", unusable)
        assert stale_after() == 7 * DAY, unusable


def test_unique_work_in_a_scratch_repository_snapshots_with_a_removal_fallback(repo, tmp_path):
    tree = repo.worktree("fixture", base=tmp_path / "scratch" / "task")
    (tree / "notes.txt").write_text("unsaved\n")
    age(tree, 25 * HOUR)
    finding = classify(tree, Root("scratchpad", str(tmp_path), "scratch"), snap())
    assert (finding.action, finding.reason, finding.disposable) == (
        "snapshot",
        "unique work, owner gone: push to wip/ then remove; scratch repository: removed if the push fails",
        True,
    )
    outside = classify(tree, Root("scratchpad", str(tmp_path / "scratch"), "scratch"), snap())
    assert (outside.action, outside.reason, outside.disposable) == (
        "snapshot",
        "unique work, owner gone: push to wip/ then remove",
        False,
    )
    managed = classify(tree, Root("worktrees", str(tmp_path), "worktrees"), snap())
    assert (managed.action, managed.disposable) == ("snapshot", False)
    age(tree, 3 * HOUR)
    recent = classify(tree, Root("scratchpad", str(tmp_path), "scratch"), snap())
    assert (recent.action, recent.reason, recent.disposable) == ("keep", "unique work, recent", False)
