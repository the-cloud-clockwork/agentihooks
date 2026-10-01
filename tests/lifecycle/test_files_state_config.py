import json
import os
import time

from hooks.lifecycle.config import load_roots
from hooks.lifecycle.files import classify_files
from hooks.lifecycle.model import Finding, Root
from hooks.lifecycle.state import confirm

from .conftest import snap

DAY = 86400


def old(path, days):
    stamp = time.time() - days * DAY
    os.utime(path, (stamp, stamp))


def test_ttl_and_archive_roots_select_idle_entries_only(tmp_path):
    exchange = tmp_path / "exchange"
    exchange.mkdir()
    for name, days in (("old.json", 20), ("new.json", 2), (".hidden", 30)):
        (exchange / name).write_text("{}")
        old(exchange / name, days)
    ttl = classify_files(Root("x", str(exchange), "ttl", 14), snap())
    assert [(os.path.basename(f.path), f.action) for f in ttl] == [("old.json", "remove")]
    chat = tmp_path / "chat"
    (chat / "archive").mkdir(parents=True)
    for name in ("chat-1.txt", "PROTOCOL.md"):
        (chat / name).write_text("t")
        old(chat / name, 30)
    archived = classify_files(Root("y", str(chat), "archive", 14, include=("chat-*.txt",)), snap())
    assert [(os.path.basename(f.path), f.action) for f in archived] == [("chat-1.txt", "archive")]
    assert classify_files(Root("y", str(chat), "archive", 14, include=("chat-*.txt",)), snap(uptime=5)) == []


def test_actions_become_due_only_after_an_hour_in_the_same_boot(tmp_path):
    state = tmp_path / "gc-state.json"
    item = Finding("/w/a", "r", "worktree", "remove", "x")
    keep = Finding("/w/b", "r", "worktree", "keep", "y")
    first = confirm([item, keep], snap(uptime=10_000), state)
    assert [f.due for f in first] == [False, False]
    assert confirm([item], snap(uptime=12_000), state)[0].due is False
    assert confirm([item], snap(uptime=13_700), state)[0].due is True
    assert json.loads(state.read_text())["seen"]["/w/a"]["uptime"] == 10_000


def test_boot_change_or_action_change_resets_confirmation(tmp_path):
    from dataclasses import replace

    state = tmp_path / "gc-state.json"
    item = Finding("/w/a", "r", "worktree", "remove", "x")
    confirm([item], snap(uptime=10_000), state)
    rebooted = replace(snap(uptime=20_000), boot_id="boot-2")
    assert confirm([item], rebooted, state)[0].due is False
    snapshot_item = replace(item, action="snapshot")
    assert confirm([snapshot_item], replace(snap(uptime=30_000), boot_id="boot-2"), state)[0].due is False


def test_roots_merge_by_id_and_drop_disabled_or_invalid(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    overlay = tmp_path / "lifecycle.json"
    overlay.write_text(
        json.dumps(
            {
                "roots": [
                    {"id": "scratchpad", "budget_gb": 10},
                    {"id": "chat-state", "enabled": False},
                    {"id": "bad", "path": "~/x", "kind": "nope"},
                    {"id": "extra", "path": "~/extra", "kind": "ttl", "idle_days": 3},
                ]
            }
        )
    )
    roots = {root.id: root for root in load_roots([overlay])}
    assert roots["scratchpad"].budget_gb == 10 and roots["scratchpad"].idle_days == 7
    assert roots["scratchpad"].path == str(tmp_path / "scratchpad")
    assert "chat-state" not in roots and "bad" not in roots
    assert roots["extra"].kind == "ttl"
    assert roots["worktrees"].path == str(tmp_path / "dev" / "worktrees")
