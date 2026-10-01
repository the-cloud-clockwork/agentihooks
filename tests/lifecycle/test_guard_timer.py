import os
import time

import pytest

from hooks.lifecycle import guard, run, timer
from hooks.lifecycle.lease import read_lease
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
    monkeypatch.setattr(timer, "start_now", lambda: started.append(1) or True)
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


def test_timer_units_render_and_install_into_a_fake_home(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_HOME", "/srv/ah")
    service, unit = timer.render("/venv/bin/python", "/opt/agentihooks")
    assert "ExecStart=/venv/bin/python -m scripts.gc_cli gc --enforce" in service
    assert "WorkingDirectory=/opt/agentihooks" in service and "Environment=AGENTIHOOKS_HOME=/srv/ah" in service
    assert "OnBootSec=2h" in unit and "OnUnitActiveSec=1h" in unit
    assert "not enabled" in timer.install_timer("/venv/bin/python", "/opt/agentihooks")
    assert timer.installed()
    assert timer.start_now() is False
    assert timer.remove_timer() == "[OK] Removed agentihooks-gc.timer"
    assert not timer.installed()


def test_session_event_kicks_only_when_enabled(monkeypatch):
    calls = []
    monkeypatch.setattr(guard, "kick", lambda: calls.append(time.time()))
    guard.session_event()
    monkeypatch.setenv("LIFECYCLE_GC_ENABLED", "true")
    guard.session_event()
    assert len(calls) == 1
