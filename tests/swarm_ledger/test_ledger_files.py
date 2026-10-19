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
