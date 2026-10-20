import json
import os
import shutil
import socket
import tempfile
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from hooks.lifecycle import codex_homes, run
from hooks.lifecycle.liveness import Snapshot
from hooks.proc import Process

ROLLOUT = Path("2026") / "10" / "16" / "rollout-a.jsonl"


def later(table=None, cwds=(), seconds=codex_homes.SETTLE_S + 60) -> Snapshot:
    return Snapshot("boot-1", 99999.0, time.time() + seconds, table or {}, cwds, {})


def plant(root: Path, name: str, transcript: bytes = b'{"turn":1}\n') -> Path:
    directory = root / name
    home = directory / "codex"
    (home / "sessions" / ROLLOUT).parent.mkdir(parents=True)
    (home / "sessions" / ROLLOUT).write_bytes(transcript)
    (home / ".tmp" / "plugins").mkdir(parents=True)
    (home / ".tmp" / "plugins" / "cache.bin").write_bytes(b"x" * 8192)
    canonical = root.parent / "canonical"
    (home / "config.toml").write_text(f'sqlite_home = "{canonical}"\n')
    return directory


@pytest.fixture
def layout(tmp_path):
    environ = {"XDG_RUNTIME_DIR": str(tmp_path / "run"), "HOME": str(tmp_path / "home")}
    legacy = tmp_path / "run" / "cx"
    current = tmp_path / "home" / ".agentihooks" / "cx"
    legacy.mkdir(parents=True)
    current.mkdir(parents=True)
    state = tmp_path / "state"
    state.mkdir()
    return environ, legacy, current, state, tmp_path / "proc"


def test_roots_are_the_legacy_runtime_folder_and_the_current_runtime_home():
    environ = {"XDG_RUNTIME_DIR": "/run/user/7", "HOME": "/home/u"}
    assert codex_homes.roots(environ) == [Path("/run/user/7/cx"), Path("/home/u/.agentihooks/cx")]


def test_orphan_homes_keep_their_transcripts_and_are_removed(layout):
    environ, legacy, current, state, proc = layout
    old = plant(legacy, "69aede3172ac4d86")
    new = plant(current, "14812a3b04ea431c", b'{"turn":2}\n')
    canonical = legacy.parent / "canonical"
    result = codex_homes.sweep_codex_homes(later(), state, act=True, proc=proc, environ=environ)
    assert [item["path"] for item in result] == [str(old), str(new)]
    assert [item["action"] for item in result] == ["removed", "removed"]
    assert all(item["bytes"] >= 8192 for item in result)
    assert not old.exists()
    assert not new.exists()
    assert (canonical / "sessions" / ROLLOUT).read_bytes() == b'{"turn":1}\n'
    assert (current.parent / "canonical" / "sessions" / ROLLOUT).read_bytes() == b'{"turn":2}\n'
    logged = [json.loads(line) for line in (state / codex_homes.LOG).read_text().splitlines()]
    assert logged == result


def test_dry_run_reports_orphans_and_leaves_them(layout):
    environ, legacy, _, state, proc = layout
    old = plant(legacy, "69aede3172ac4d86")
    result = codex_homes.sweep_codex_homes(later(), state, proc=proc, environ=environ)
    assert [(item["path"], item["action"]) for item in result] == [(str(old), "would remove")]
    assert (old / "codex" / "sessions" / ROLLOUT).exists()
    assert not (state / codex_homes.LOG).exists()


def test_a_missing_legacy_root_still_sweeps_the_current_root(layout):
    environ, legacy, current, state, proc = layout
    legacy.rmdir()
    kept = plant(current, "14812a3b04ea431c")
    result = codex_homes.sweep_codex_homes(later(), state, proc=proc, environ=environ)
    assert [item["path"] for item in result] == [str(kept)]


def test_a_home_younger_than_the_settle_window_is_left_alone(layout):
    environ, legacy, _, state, proc = layout
    young = plant(legacy, "69aede3172ac4d86")
    result = codex_homes.sweep_codex_homes(later(seconds=0), state, act=True, proc=proc, environ=environ)
    assert result == []
    assert young.exists()


def test_a_recent_write_deep_in_an_old_home_keeps_it(layout):
    environ, legacy, _, state, proc = layout
    busy = plant(legacy, "69aede3172ac4d86")
    stamp = time.time() - codex_homes.SETTLE_S - 60
    for item in [busy, *busy.rglob("*")]:
        os.utime(item, (stamp, stamp), follow_symlinks=False)
    (busy / "codex" / ".tmp" / "plugins" / "cache.bin").write_bytes(b"y")
    result = codex_homes.sweep_codex_homes(later(seconds=0), state, act=True, proc=proc, environ=environ)
    assert result == []
    assert busy.exists()


def test_a_home_gone_before_its_walk_is_not_reported(layout, monkeypatch):
    environ, legacy, _, state, proc = layout
    plant(legacy, "a1")
    monkeypatch.setattr(codex_homes, "walk_stats", lambda directory: (0.0, 0))
    removed = []
    monkeypatch.setattr(codex_homes, "remove_home", removed.append)
    assert codex_homes.sweep_codex_homes(later(), state, act=True, proc=proc, environ=environ) == []
    assert removed == []


def test_a_failed_log_write_keeps_the_removal_and_the_sweep(layout):
    environ, legacy, _, _, proc = layout
    gone = plant(legacy, "a1")
    result = codex_homes.sweep_codex_homes(later(), legacy / "missing", act=True, proc=proc, environ=environ)
    assert [(item["path"], item["action"]) for item in result] == [(str(gone), "removed")]
    assert "No such file" in result[0]["log_error"]
    assert not gone.exists()


def test_a_malformed_config_is_reported_and_the_sweep_goes_on(layout):
    environ, legacy, _, state, proc = layout
    broken = plant(legacy, "a1")
    (broken / "codex" / "config.toml").write_text("sqlite_home = [\n")
    good = plant(legacy, "b2")
    result = codex_homes.sweep_codex_homes(later(), state, act=True, proc=proc, environ=environ)
    assert [(item["path"], item["action"]) for item in result] == [(str(broken), "error"), (str(good), "removed")]
    assert broken.exists()


@pytest.mark.parametrize("kind", ["cwd", "argv", "fd"])
def test_a_home_a_live_process_refers_to_is_never_touched(layout, kind):
    environ, legacy, _, state, proc = layout
    owned = plant(legacy, "bf195b3268c5448c")
    orphan = plant(legacy, "c4728002b1764ca5")
    argv = ("codex", "resume", "--remote", f"unix://{owned}/s") if kind == "argv" else ("codex",)
    row = Process(42, 1, 42, 42, 100, "S", "codex", argv)
    (proc / "42" / "fd").mkdir(parents=True)
    if kind == "fd":
        (proc / "42" / "fd" / "3").symlink_to(owned / "codex" / "config.toml")
    cwds = (str(owned / "codex"),) if kind == "cwd" else ()
    result = codex_homes.sweep_codex_homes(later({42: row}, cwds), state, act=True, proc=proc, environ=environ)
    assert [item["path"] for item in result] == [str(orphan)]
    assert (owned / "codex" / "sessions" / ROLLOUT).exists()
    assert not orphan.exists()


def test_unreadable_process_entries_do_not_hide_later_references(layout):
    environ, legacy, _, state, proc = layout
    owned = plant(legacy, "a1")
    orphan = plant(legacy, "b2")
    (proc / "42" / "fd").mkdir(parents=True)
    (proc / "42" / "fd" / "3").write_text("not a link")
    (proc / "42" / "fd" / "4").symlink_to(owned / "codex" / "config.toml")
    table = {
        41: Process(41, 1, 41, 41, 100, "S", "sh", ("sh",)),
        42: Process(42, 1, 42, 42, 100, "S", "codex", ("codex",)),
    }
    result = codex_homes.sweep_codex_homes(later(table), state, act=True, proc=proc, environ=environ)
    assert [item["path"] for item in result] == [str(orphan)]
    assert owned.exists()


def test_a_young_home_does_not_stop_the_sweep_of_older_ones(layout):
    environ, legacy, _, state, proc = layout
    young = plant(legacy, "a1")
    old = plant(legacy, "b2")
    stamp = time.time() - codex_homes.SETTLE_S - 60
    for item in [old, *old.rglob("*")]:
        os.utime(item, (stamp, stamp), follow_symlinks=False)
    result = codex_homes.sweep_codex_homes(later(seconds=0), state, act=True, proc=proc, environ=environ)
    assert [item["path"] for item in result] == [str(old)]
    assert young.exists()
    assert not old.exists()


def test_the_socket_probe_is_bounded_and_a_timeout_reads_as_not_listening(monkeypatch):
    probe = Mock()
    probe.__enter__ = Mock(return_value=probe)
    probe.__exit__ = Mock(return_value=False)
    probe.connect.side_effect = TimeoutError("timed out")
    opened = Mock(return_value=probe)
    monkeypatch.setattr(codex_homes.socket, "socket", opened)
    assert codex_homes.listening(Path("/run/user/7/cx/a1/s")) is False
    opened.assert_called_once_with(socket.AF_UNIX)
    probe.settimeout.assert_called_once_with(1)
    probe.connect.assert_called_once_with("/run/user/7/cx/a1/s")


def test_a_listening_server_socket_marks_its_home_owned(layout):
    environ, _, _, state, proc = layout
    with tempfile.TemporaryDirectory(dir="/tmp") as short:
        legacy = Path(short) / "run" / "cx"
        legacy.mkdir(parents=True)
        owned = plant(legacy, "a1")
        dead = plant(legacy, "b2")
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(dead / "s"))
        stale.close()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(owned / "s"))
            server.listen(1)
            result = codex_homes.sweep_codex_homes(
                later(), state, act=True, proc=proc, environ={**environ, "XDG_RUNTIME_DIR": str(Path(short) / "run")}
            )
        assert [item["path"] for item in result] == [str(dead)]
        assert owned.exists()
        assert not dead.exists()


def test_scope_limits_the_homes_considered(layout):
    environ, legacy, current, state, proc = layout
    plant(legacy, "69aede3172ac4d86")
    kept = plant(current, "14812a3b04ea431c")
    result = codex_homes.sweep_codex_homes(later(), state, str(kept), act=True, proc=proc, environ=environ)
    assert [item["path"] for item in result] == [str(kept)]
    assert (legacy / "69aede3172ac4d86").exists()


def test_symlinked_roots_and_entries_are_skipped(layout, tmp_path):
    environ, legacy, _, state, proc = layout
    target = plant(tmp_path / "other" / "cx", "elsewhere")
    (legacy / "linked").symlink_to(target)
    (legacy / "file").write_text("x")
    assert codex_homes.sweep_codex_homes(later(), state, act=True, proc=proc, environ=environ) == []
    assert target.exists()


def test_a_failed_removal_is_reported_and_the_sweep_goes_on(layout, monkeypatch):
    environ, legacy, _, state, proc = layout
    first = plant(legacy, "a1")
    second = plant(legacy, "b2")
    removed = []

    def remove(directory):
        if directory == first:
            raise PermissionError("denied")
        removed.append(directory)
        shutil.rmtree(directory)

    monkeypatch.setattr(codex_homes, "remove_home", remove)
    result = codex_homes.sweep_codex_homes(later(), state, act=True, proc=proc, environ=environ)
    assert result[0] == {"path": str(first), "action": "error", "error": "denied"}
    assert result[1]["action"] == "removed"
    assert removed == [second]
    assert [json.loads(line)["path"] for line in (state / codex_homes.LOG).read_text().splitlines()] == [str(second)]


def test_a_home_left_on_disk_is_reported_as_an_error_and_not_logged(layout, monkeypatch):
    environ, legacy, _, state, proc = layout
    stuck = plant(legacy, "a1")
    monkeypatch.setattr(codex_homes, "remove_home", lambda directory: None)
    result = codex_homes.sweep_codex_homes(later(), state, act=True, proc=proc, environ=environ)
    assert result == [{"path": str(stuck), "action": "error", "error": "home still present after removal"}]
    assert stuck.exists()
    assert not (state / codex_homes.LOG).exists()


def test_an_unreadable_root_is_skipped_and_the_other_root_is_swept(layout, monkeypatch):
    environ, legacy, current, state, proc = layout
    plant(legacy, "a1")
    kept = plant(current, "b2")
    listed = Path.iterdir

    def iterdir(path):
        if path == legacy:
            raise PermissionError("denied")
        return listed(path)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    result = codex_homes.sweep_codex_homes(later(), state, proc=proc, environ=environ)
    assert [item["path"] for item in result] == [str(kept)]


def test_lifecycle_sweep_reports_codex_homes_with_its_snapshot_scope_and_action(tmp_path, monkeypatch):
    snapshot = later()
    cleanup = Mock(return_value=[{"path": "/run/user/1/cx/a", "action": "removed"}])
    monkeypatch.setattr(run, "sweep_codex_homes", cleanup)
    monkeypatch.setattr(run, "sweep_servers", Mock(return_value=[]))
    report = run.sweep([], snapshot, tmp_path, scope="/run", act=True)
    cleanup.assert_called_once_with(snapshot, tmp_path, "/run", True)
    assert report["codex_homes"] == [{"path": "/run/user/1/cx/a", "action": "removed"}]
