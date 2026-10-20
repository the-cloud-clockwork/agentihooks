from pathlib import Path

from hooks.lifecycle.evict import free_gb, make_room

from hooks.lifecycle.lease import add_holder
from hooks.lifecycle.model import Holder

from .conftest import age, git, process, snap

HOUR = 3600


def plenty(_path):
    return 1000


def wip_refs(repo):
    return git(repo.origin, "for-each-ref", "--format=%(refname)", "refs/heads/wip")


def test_full_cap_evicts_the_oldest_safe_worktree_first(repo, tmp_path):
    old = repo.worktree("old")
    new = repo.worktree("new")
    age(old, 5 * HOUR)
    age(new, HOUR)
    lines = make_room(repo.primary, repo.root(), 0, 2, tmp_path / "home", snap(), plenty)
    assert lines == [f"evicted {old}: removed"]
    assert not old.exists() and new.exists()
    assert make_room(repo.primary, repo.root(), 0, 2, tmp_path / "home", snap(), plenty) == []


def test_unique_work_is_pushed_to_wip_before_eviction(repo, tmp_path):
    work = repo.worktree("work")
    (work / "notes.txt").write_text("unsaved\n")
    lines = make_room(repo.primary, repo.root(), 0, 1, tmp_path / "home", snap(), plenty)
    [ref] = wip_refs(repo).split()
    assert ref.startswith("refs/heads/wip/primary/work-")
    assert lines == [f"evicted {work}: pushed {ref.removeprefix('refs/heads/')}; removed"]
    assert not work.exists()


def test_held_worktrees_are_never_evicted(repo, tmp_path):
    main = repo.worktree("main")
    used = repo.worktree("used")
    owned = repo.worktree("owned")
    add_holder(owned, "worktree", Holder("", 4242, 77, "boot-1"), 0.0)
    for path in (main, used, owned):
        age(path, 30 * HOUR)
    view = snap(cwds=(str(used),), table={4242: process(4242, start=77)}, uptime=60)
    assert make_room(repo.primary, repo.root(), 0, 1, tmp_path / "home", view, plenty) == []
    assert main.exists() and used.exists() and owned.exists()


def test_boot_grace_does_not_hold_an_eviction(repo, tmp_path):
    done = repo.worktree("done")
    lines = make_room(repo.primary, repo.root(), 0, 1, tmp_path / "home", snap(uptime=60), plenty)
    assert lines == [f"evicted {done}: removed"]


def other_repository(repo, tmp_path):
    clone = tmp_path / "second"
    git(tmp_path, "clone", "-q", str(repo.origin), str(clone))
    tree = repo.trees / "second" / "theirs"
    git(clone, "worktree", "add", "-q", "--no-track", "-b", "theirs", str(tree), "origin/dev")
    return tree


def test_free_space_floor_evicts_across_repos_until_met(repo, tmp_path):
    theirs = other_repository(repo, tmp_path)
    first = repo.worktree("first")
    second = repo.worktree("second")
    age(theirs, 9 * HOUR)
    age(first, 5 * HOUR)
    age(second, HOUR)

    def free(path):
        assert path == repo.trees
        return 60 if not first.exists() else 10

    lines = make_room(repo.primary, repo.root(), 50, 60, tmp_path / "home", snap(), free)
    assert lines == [f"evicted {theirs}: removed", f"evicted {first}: removed"]
    assert second.exists()


def test_cap_alone_evicts_only_the_named_repository(repo, tmp_path):
    theirs = other_repository(repo, tmp_path)
    mine = repo.worktree("mine")
    age(theirs, 9 * HOUR)
    age(mine, HOUR)
    lines = make_room(repo.primary, repo.root(), 0, 1, tmp_path / "home", snap(), plenty)
    assert lines == [f"evicted {mine}: removed"]
    assert theirs.exists()


def test_failed_push_keeps_the_worktree_and_moves_on(repo, tmp_path):
    stuck = repo.worktree("stuck")
    (stuck / "notes.txt").write_text("unsaved\n")
    clean = repo.worktree("clean")
    age(stuck, 5 * HOUR)
    age(clean, HOUR)
    git(repo.primary, "remote", "set-url", "--push", "origin", str(tmp_path / "missing.git"))
    lines = make_room(repo.primary, repo.root(), 0, 2, tmp_path / "home", snap(), plenty)
    assert lines[0].startswith(f"kept {stuck}: git push: ")
    assert lines[1:] == [f"evicted {clean}: removed"]
    assert stuck.exists() and not clean.exists()


def test_free_gb_takes_the_smaller_filesystem_and_skips_missing_ones(tmp_path, monkeypatch):
    from collections import namedtuple

    from hooks.lifecycle import evict

    usage = namedtuple("usage", "total used free")
    mount = tmp_path / "mnt"
    mount.mkdir()
    sizes = {tmp_path: 80 << 30, mount: (30 << 30) + (1 << 29)}
    monkeypatch.setattr(evict.shutil, "disk_usage", lambda path: usage(0, 0, sizes[Path(path)]))
    monkeypatch.setattr(evict, "EXTRA_FILESYSTEMS", (str(mount),))
    assert free_gb(tmp_path / "absent" / "deeper") == 30
    monkeypatch.setattr(evict, "EXTRA_FILESYSTEMS", (str(tmp_path / "nowhere"),))
    assert free_gb(tmp_path) == 80
