import fcntl
import json

from hooks.lifecycle.model import Holder, Root
from hooks.lifecycle.run import sweep
from scripts import gc_cli

from .conftest import age, snap


def test_sweep_writes_report_and_state_and_scopes(repo, tmp_path):
    keep = repo.worktree("keep-me")
    gone = repo.worktree("gone")
    age(gone, 5 * 3600)
    home = tmp_path / "state"
    report = sweep([repo.root()], snap(cwds=(str(keep),)), home)
    actions = {item["path"]: item["action"] for item in report["findings"]}
    assert actions == {str(keep): "keep", str(gone): "remove"}
    assert report["totals"]["remove"]["count"] == 1
    assert json.loads((home / "gc-last.json").read_text())["totals"] == report["totals"]
    assert str(gone) in json.loads((home / "gc-state.json").read_text())["seen"]
    scoped = sweep([repo.root()], snap(cwds=(str(keep),)), home, scope=str(keep))
    assert [item["path"] for item in scoped["findings"]] == [str(keep)]


def test_sweep_runs_server_cleanup_with_the_same_snapshot_scope_and_action(tmp_path, monkeypatch):
    from unittest.mock import Mock

    from hooks.lifecycle import run

    snapshot = snap()
    cleanup = Mock(return_value=[{"pid": 42, "action": "stopped"}])
    monkeypatch.setattr(run, "sweep_servers", cleanup)
    report = sweep([], snapshot, tmp_path, scope="/run", act=True)
    cleanup.assert_called_once_with(snapshot.table, tmp_path, "/run", True)
    assert report["ledger_servers"] == [{"pid": 42, "action": "stopped"}]


def test_sweep_refuses_to_run_twice_at_once(tmp_path):
    home = tmp_path / "state"
    home.mkdir()
    with open(home / "gc.lock", "a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert sweep([], snap(), home) == {"skipped": "another sweep is running"}


def test_scratch_new_creates_a_leased_dir(monkeypatch, tmp_path, capsys):
    for key in ("AGENTIHOOKS_AGENT_NAME", "AGENTIHOOKS_SWARM", "AGENTIHOOKS_SWARM_TASK", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(gc_cli, "take_snapshot", lambda: snap())
    monkeypatch.setattr(gc_cli, "owner_holder", lambda _snap: Holder("s-1", 9, 9, "boot-1"))
    assert gc_cli.main(["scratch", "new", "repo/task"]) == 0
    created = capsys.readouterr().out.strip()
    assert created.endswith("scratchpad/repo/task")
    lease = json.loads((tmp_path / "_home" / "scratchpad" / "repo" / "task" / ".lease.json").read_text())
    assert lease["holders"][0]["session_id"] == "s-1"
    assert gc_cli.main(["scratch", "new", "../escape"]) == 1


def test_lease_requires_a_linked_worktree_and_an_agent(repo, monkeypatch, capsys):
    path = repo.worktree("leased")
    monkeypatch.setattr(gc_cli, "take_snapshot", lambda: snap())
    monkeypatch.setattr(gc_cli, "owner_holder", lambda _snap: None)
    assert gc_cli.main(["lease", str(repo.primary)]) == 1
    assert gc_cli.main(["lease", str(path)]) == 0
    assert "no agent session" in capsys.readouterr().err
    monkeypatch.setattr(gc_cli, "owner_holder", lambda _snap: Holder("", 5, 5, "boot-1"))
    assert gc_cli.main(["lease", str(path)]) == 0
    assert (repo.primary / ".git" / "worktrees" / "leased" / "agentihooks-lease.json").exists()


def test_gc_prints_actionable_lines(monkeypatch, capsys):
    report = {
        "totals": {"keep": {"count": 2, "bytes": 0}, "remove": {"count": 1, "bytes": 1 << 30}},
        "findings": [
            {"path": "/w/a", "action": "remove", "reason": "clean and on a remote", "size": 1 << 30, "due": True},
            {"path": "/w/b", "action": "keep", "reason": "recent", "size": 0, "due": False},
        ],
    }
    monkeypatch.setattr(gc_cli, "sweep", lambda scope="", act=False: report)
    assert gc_cli.main(["gc"]) == 0
    out = capsys.readouterr().out
    assert "remove    due" in out and "/w/a" in out and "/w/b" not in out


def test_unused_root_kinds_do_not_break_the_sweep(tmp_path):
    root = Root("missing", str(tmp_path / "absent"), "scratch", 7, 1)
    report = sweep([root], snap(), tmp_path / "state")
    assert report["findings"] == []
