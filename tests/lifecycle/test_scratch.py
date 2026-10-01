import os
import time

from hooks.lifecycle.lease import add_holder
from hooks.lifecycle.model import Holder, Root
from hooks.lifecycle.scratch import classify_scratch

from .conftest import process, snap

DAY = 86400


def task(root, name, days, size=0):
    path = root / name
    path.mkdir(parents=True)
    (path / "data.bin").write_bytes(b"x" * size)
    stamp = time.time() - days * DAY
    for item in (path / "data.bin", path):
        os.utime(item, (stamp, stamp))
    return path


def verdicts(root, view=None, held=frozenset(), budget_gb=0.0):
    found = classify_scratch(Root("scratchpad", str(root), "scratch", 7, budget_gb), view or snap(), set(held))
    return {os.path.relpath(item.path, root): (item.action, item.reason) for item in found}


def test_idle_dirs_go_recent_and_pinned_stay(tmp_path):
    root = tmp_path / "scratchpad"
    task(root, "repo/old", 8)
    task(root, "repo/new", 1)
    pinned = task(root, "repo/pinned", 30)
    (pinned / ".keep").touch()
    os.utime(pinned / ".keep", (time.time() - 30 * DAY,) * 2)
    (root / "repo" / "loose.log").write_text("depth-2 file\n")
    (root / "defer.md").write_text("depth-1 file\n")
    assert verdicts(root) == {
        "repo/new": ("keep", "recent"),
        "repo/old": ("remove", "idle over 7 days"),
        "repo/pinned": ("keep", "pinned (.keep)"),
    }


def test_owner_cwd_nested_work_and_boot_grace_hold_old_dirs(tmp_path):
    root = tmp_path / "scratchpad"
    owned = task(root, "repo/owned", 9)
    add_holder(owned, "scratch", Holder("", 70, 7, "boot-1"), 0.0)
    os.utime(owned / ".lease.json", (time.time() - 9 * DAY,) * 2)
    used = task(root, "repo/used", 9)
    nested = task(root, "repo/nested", 9)
    view = snap(table={70: process(70, start=7)}, cwds=(str(used),))
    found = verdicts(root, view, held={str(nested / "wt")})
    assert found["repo/owned"] == ("keep", "owner session alive")
    assert found["repo/used"] == ("keep", "a process works inside")
    assert found["repo/nested"] == ("keep", "holds a worktree with work")
    assert set(verdicts(root, snap(uptime=60)).values()) == {("keep", "boot grace")}


def test_budget_evicts_oldest_dead_dirs_first(tmp_path):
    root = tmp_path / "scratchpad"
    for name, days in (("a", 6), ("b", 5), ("c", 4), ("d", 1)):
        task(root, f"repo/{name}", days, size=300_000)
    pinned = task(root, "repo/z-pinned", 6, size=300_000)
    (pinned / ".keep").touch()
    found = verdicts(root, budget_gb=0.9 / 1024)
    assert found["repo/a"] == ("remove", "over budget, oldest first")
    assert found["repo/b"] == ("remove", "over budget, oldest first")
    assert found["repo/c"] == ("keep", "recent")
    assert found["repo/z-pinned"] == ("keep", "pinned (.keep)")
