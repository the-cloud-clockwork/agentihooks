import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))

from scripts.swarm_ledger import ledger_core as core  # noqa: E402
from scripts.swarm_ledger import ledger_files  # noqa: E402


@pytest.mark.parametrize("name", ("LOG_MAX_BYTES", "rotate_if_full", "append_capped", "atomic_write"))
def test_core_serves_the_file_write_names(name):
    assert getattr(core, name) is getattr(ledger_files, name)


def test_the_log_cap_is_five_megabytes():
    assert ledger_files.LOG_MAX_BYTES == 5 * 1024 * 1024


def test_rotate_if_full_moves_a_full_file_aside(tmp_path):
    log = tmp_path / "server.log"
    log.write_text("x" * 10)
    ledger_files.rotate_if_full(log, limit=10)
    assert (log.exists(), (tmp_path / "server.log.1").read_text()) == (False, "x" * 10)


def test_rotate_if_full_keeps_a_file_under_the_limit(tmp_path):
    log = tmp_path / "server.log"
    log.write_text("x" * 9)
    ledger_files.rotate_if_full(log, limit=10)
    assert (log.read_text(), (tmp_path / "server.log.1").exists()) == ("x" * 9, False)


def test_rotate_if_full_ignores_a_missing_file(tmp_path):
    folder = tmp_path / "logs"
    folder.mkdir()
    ledger_files.rotate_if_full(folder / "absent.log", limit=1)
    assert list(folder.iterdir()) == []


def test_append_capped_appends_then_rotates_at_the_limit(tmp_path):
    log = tmp_path / "hook.log"
    ledger_files.append_capped(log, "ab", limit=4)
    ledger_files.append_capped(log, "cd", limit=4)
    assert log.read_text() == "abcd"
    ledger_files.append_capped(log, "é", limit=4)
    assert (log.read_text(encoding="utf-8"), (tmp_path / "hook.log.1").read_text()) == ("é", "abcd")


def test_atomic_write_replaces_the_file_and_returns_the_written_stat(tmp_path):
    folder = tmp_path / "ledgers"
    folder.mkdir()
    target = folder / "ledger.json"
    target.write_text("old")
    written = ledger_files.atomic_write(target, "né")
    assert target.read_text(encoding="utf-8") == "né"
    assert (written.st_size, written.st_ino) == (len("né".encode()), target.stat().st_ino)
    assert sorted(p.name for p in folder.iterdir()) == ["ledger.json"]


def test_atomic_write_stages_a_hidden_utf8_file_beside_the_target(tmp_path, monkeypatch):
    folder = tmp_path / "ledgers"
    folder.mkdir()
    staged, opened = [], []
    replace, fdopen = os.replace, os.fdopen
    monkeypatch.setattr(os, "replace", lambda src, dst: staged.append(Path(src)) or replace(src, dst))
    monkeypatch.setattr(
        os, "fdopen", lambda fd, *args, **kwargs: opened.append((args, kwargs)) or fdopen(fd, *args, **kwargs)
    )
    ledger_files.atomic_write(folder / "ledger.json", "x")
    assert [(path.parent, path.name.startswith(".ledger.json.")) for path in staged] == [(folder, True)]
    assert opened == [(("w",), {"encoding": "utf-8"})]


def test_append_capped_opens_the_log_for_utf8_appends(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(
        ledger_files,
        "open",
        lambda *args, **kwargs: opened.append((args, kwargs)) or open(*args, **kwargs),
        raising=False,
    )
    ledger_files.append_capped(tmp_path / "hook.log", "x", limit=10)
    assert opened == [((tmp_path / "hook.log", "a"), {"encoding": "utf-8"})]
