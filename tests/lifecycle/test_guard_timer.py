import os
import time
from pathlib import Path

import pytest

from hooks.lifecycle import guard, run
from hooks.lifecycle.lease import add_holder, read_lease
from hooks.lifecycle.locks import being_removed, removing
from hooks.lifecycle.model import Holder, Root

from .conftest import age, snap


@pytest.fixture
def owner(monkeypatch):
    holder = Holder("sess-1", 4242, 7, "boot-1")
    monkeypatch.setattr(guard, "caller_holder", lambda session_id: holder)
    return holder


def roots_for(repo, tmp_path):
    return [repo.root(), Root("scratchpad", str(tmp_path / "scratch"), "scratch")]


def test_claim_records_the_caller_on_worktrees_and_scratch_dirs(repo, tmp_path, owner):
    path = repo.worktree("task")
    task = tmp_path / "scratch" / "proj" / "t1"
    task.mkdir(parents=True)
    payload = {"session_id": "sess-1", "tool_input": {"command": f"cd {path}/src && ls {task}/out"}}
    assert guard.claim(payload, roots_for(repo, tmp_path), tmp_path / "home") == ""
    assert read_lease(path, "worktree").holders == (owner,)
    assert read_lease(task, "scratch").holders == (owner,)
    edit = {"session_id": "sess-1", "tool_input": {"file_path": str(path / "a.py")}}
    lease_file = repo.primary / ".git" / "worktrees" / "task" / "agentihooks-lease.json"
    before = lease_file.stat().st_mtime_ns
    guard.claim(edit, roots_for(repo, tmp_path), tmp_path / "home")
    assert lease_file.stat().st_mtime_ns == before


def test_claim_records_the_caller_on_a_bash_workdir(repo, tmp_path, owner):
    path = repo.worktree("task")
    payload = {"session_id": "sess-1", "tool_input": {"command": "git status", "workdir": str(path)}}
    assert guard.claim(payload, roots_for(repo, tmp_path), tmp_path / "home") == ""
    assert read_lease(path, "worktree").holders == (owner,)


def test_claim_replaces_a_dead_holder_of_the_same_session(repo, tmp_path, owner):
    path = repo.worktree("task")
    add_holder(path, "worktree", Holder("sess-1", 2**22 + 1, 3, "boot-1"), time.time())
    payload = {"session_id": "sess-1", "tool_input": {"file_path": str(path / "a.py")}}
    assert guard.claim(payload, roots_for(repo, tmp_path), tmp_path / "home") == ""
    assert read_lease(path, "worktree").holders == (owner,)


def test_claim_keeps_a_running_holder_of_the_same_session_without_a_walk(repo, tmp_path, monkeypatch):
    path = repo.worktree("task")
    me = guard._process(os.getpid(), Path("/proc"))
    live = Holder("sess-1", me.pid, me.start_time, "boot-1")
    add_holder(path, "worktree", live, time.time())
    monkeypatch.setattr(guard, "caller_holder", lambda session_id: pytest.fail("walked"))
    payload = {"session_id": "sess-1", "tool_input": {"file_path": str(path / "a.py")}}
    assert guard.claim(payload, roots_for(repo, tmp_path), tmp_path / "home") == ""
    assert read_lease(path, "worktree").holders == (live,)


def test_claim_keeps_a_running_holder_beside_another_sessions_holder(repo, tmp_path, monkeypatch):
    path = repo.worktree("task")
    me = guard._process(os.getpid(), Path("/proc"))
    other, live = Holder("sess-9", 2**22 + 1, 3, "boot-1"), Holder("sess-1", me.pid, me.start_time, "boot-1")
    add_holder(path, "worktree", other, time.time())
    add_holder(path, "worktree", live, time.time())
    monkeypatch.setattr(guard, "caller_holder", lambda session_id: pytest.fail("walked"))
    payload = {"session_id": "sess-1", "tool_input": {"file_path": str(path / "a.py")}}
    assert guard.claim(payload, roots_for(repo, tmp_path), tmp_path / "home") == ""
    assert read_lease(path, "worktree").holders == (other, live)


def test_claim_replaces_a_same_session_holder_whose_pid_was_reused(repo, tmp_path, owner):
    path = repo.worktree("task")
    me = guard._process(os.getpid(), Path("/proc"))
    add_holder(path, "worktree", Holder("sess-1", me.pid, me.start_time + 1, "boot-1"), time.time())
    payload = {"session_id": "sess-1", "tool_input": {"file_path": str(path / "a.py")}}
    assert guard.claim(payload, roots_for(repo, tmp_path), tmp_path / "home") == ""
    assert read_lease(path, "worktree").holders == (owner,)


def test_claim_without_a_session_still_records_the_caller(repo, tmp_path, monkeypatch):
    path = repo.worktree("task")
    me = guard._process(os.getpid(), Path("/proc"))
    live, caller = Holder("", me.pid, me.start_time, "boot-1"), Holder("", 4242, 7, "boot-1")
    add_holder(path, "worktree", live, time.time())
    monkeypatch.setattr(guard, "caller_holder", lambda session_id: caller if session_id == "" else None)
    payload = {"session_id": "", "tool_input": {"file_path": str(path / "a.py")}}
    assert guard.claim(payload, roots_for(repo, tmp_path), tmp_path / "home") == ""
    assert read_lease(path, "worktree").holders == (live, caller)


def test_claim_writes_no_lease_without_a_caller_agent(repo, tmp_path, monkeypatch):
    path = repo.worktree("task")
    monkeypatch.setattr(guard, "caller_holder", lambda session_id: None)
    payload = {"session_id": "sess-1", "tool_input": {"command": "git status", "workdir": str(path)}}
    assert guard.claim(payload, roots_for(repo, tmp_path), tmp_path / "home") == ""
    assert read_lease(path, "worktree") is None


def test_candidate_paths_reads_every_path_field_and_the_command():
    tool_input = {"file_path": "/a", "notebook_path": "/b", "path": "/c", "workdir": "/d", "command": "ls /e"}
    assert guard.candidate_paths(tool_input) == ["/a", "/b", "/c", "/d", "/e"]


def test_claim_blocks_while_gc_removes_the_path(repo, tmp_path, owner):
    path = repo.worktree("task")
    home = tmp_path / "home"
    payload = {"session_id": "sess-2", "tool_input": {"file_path": str(path / "a.py")}}
    with removing(home, str(path)):
        assert being_removed(home, str(path))
        assert guard.claim(payload, roots_for(repo, tmp_path), home).startswith("BLOCKED: agentihooks gc is removing")
    assert not being_removed(home, str(path))
    assert guard.claim(payload, roots_for(repo, tmp_path), home) == ""


def test_managed_unit_depths_and_unmanaged_paths(repo, tmp_path):
    roots = roots_for(repo, tmp_path)
    tmp = repo.worktree("x-1", base=repo.trees / "primary" / "_tmp")
    assert guard.managed_unit(str(tmp / "deep" / "f"), roots) == (tmp, "worktree")
    assert guard.managed_unit(str(repo.trees / "primary"), roots) is None
    assert guard.managed_unit("/etc/hosts", roots) is None
    assert guard.managed_unit(str(tmp_path / "scratch" / "proj" / "missing" / "f"), roots) is None


def test_disk_warning_fires_below_the_floor_once_per_window(tmp_path, monkeypatch):
    kicked = []
    monkeypatch.setattr(guard, "free_gb", lambda: 5.0)
    monkeypatch.setattr(guard, "kick", lambda force=False, home=None: kicked.append(force))
    monkeypatch.setenv("AGENTIHOOKS_DISK_WARN_GB", "100")
    first = guard.disk_warning(tmp_path)
    assert "disk space is low (5 GB free" in first
    assert guard.disk_warning(tmp_path) == ""
    assert kicked == [True]
    monkeypatch.setattr(guard, "free_gb", lambda: 500.0)
    os.utime(tmp_path / "gc-disk-warned", (0, 0))
    assert guard.disk_warning(tmp_path) == ""


def test_kick_is_throttled_unless_forced(tmp_path, monkeypatch):
    started = []

    def forked(target, **kwargs):
        assert target is run.sweep
        assert kwargs == {"timeout_sec": 3600, "task_name": "lifecycle-gc", "act": True}
        started.append(1)

    monkeypatch.setattr("hooks._async.fork_and_call", forked)
    guard.kick(home=tmp_path)
    guard.kick(home=tmp_path)
    assert started == [1]
    guard.kick(force=True, home=tmp_path)
    assert started == [1, 1]


def test_pretool_does_nothing_when_disabled(monkeypatch):
    monkeypatch.setenv("LIFECYCLE_GC_ENABLED", "false")
    monkeypatch.setattr(guard, "claim", lambda payload: pytest.fail("claim ran"))
    assert guard.pretool({"tool_input": {"file_path": "/x"}}) == ""


def test_pre_tool_use_raises_the_lifecycle_block(monkeypatch):
    from hooks import hook_manager

    monkeypatch.setattr(guard, "pretool", lambda payload: "BLOCKED: gc")
    with pytest.raises(hook_manager.BlockAction, match="BLOCKED: gc"):
        hook_manager.on_pre_tool_use({"tool_name": "Read", "tool_input": {"file_path": "/x"}, "session_id": "s"})


def test_enforce_holds_the_removal_lock_while_acting(repo, tmp_path, monkeypatch):
    path = repo.worktree("gone")
    age(path, 3 * 3600)
    home = tmp_path / "state"
    seen = []

    def spy(item, journal):
        seen.append(being_removed(home, item.path))
        return "removed"

    monkeypatch.setattr(run, "apply", spy)
    run.sweep([repo.root()], snap(uptime=10_000), home, act=True, fresh=lambda: snap(uptime=10_000))
    run.sweep([repo.root()], snap(uptime=14_000), home, act=True, fresh=lambda: snap(uptime=14_000))
    assert seen == [True]


def test_session_event_kicks_only_when_enabled(monkeypatch):
    calls = []
    monkeypatch.setattr(guard, "kick", lambda: calls.append(time.time()))
    guard.session_event()
    monkeypatch.setenv("LIFECYCLE_GC_ENABLED", "true")
    guard.session_event()
    assert len(calls) == 1
