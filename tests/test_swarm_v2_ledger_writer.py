import json
import os
import socket
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts.swarm_ledger import ledger_server as server
from scripts.swarm_v2 import ledger_writer

EVIDENCE = Path(__file__).parents[1] / "evidence" / "SV2-LDG-04"
CLAIMANT = {"pid": 4242, "host": "writer-a", "started_at": 1000}
OTHER = {"pid": 4343, "host": "writer-b", "started_at": 2000}


@pytest.fixture
def lease(tmp_path):
    held = ledger_writer.WriterLease(tmp_path).acquire(CLAIMANT)
    yield held
    held.release()


def database(path: Path, *texts: str) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE IF NOT EXISTS rows (text TEXT)")
    connection.executemany("INSERT INTO rows VALUES (?)", [(text,) for text in texts])
    connection.commit()
    return connection


def texts(path: Path) -> list:
    connection = sqlite3.connect(path)
    try:
        return [text for (text,) in connection.execute("SELECT text FROM rows ORDER BY rowid")]
    finally:
        connection.close()


def test_the_holder_is_recorded_while_the_lease_is_held(tmp_path, lease):
    assert lease.held is True
    assert ledger_writer.holder(tmp_path) == CLAIMANT
    assert (tmp_path / ledger_writer.LOCK).read_text() == json.dumps(CLAIMANT, sort_keys=True)
    assert (tmp_path / ledger_writer.LOCK).stat().st_mode & 0o777 == 0o600


def test_a_second_writer_on_the_same_folder_is_refused_and_counted(tmp_path, lease):
    with pytest.raises(ledger_writer.WriterConflict) as refused:
        ledger_writer.WriterLease(tmp_path).acquire(OTHER)
    assert str(refused.value) == (
        f"ledger folder {tmp_path} already has an active writer, pid 4242 on writer-a; a second writer is refused"
    )
    assert refused.value.directory == tmp_path
    assert refused.value.holder == CLAIMANT
    assert ledger_writer.holder(tmp_path) == CLAIMANT
    assert ledger_writer.conflicts_total(tmp_path) == 1
    assert (tmp_path / ledger_writer.CONFLICTS).read_text() == '{"holder": 4242, "claimant": 4343, "at": 2000}\n'


def test_each_refusal_adds_one_conflict(tmp_path, lease):
    for _ in range(2):
        with pytest.raises(ledger_writer.WriterConflict):
            ledger_writer.WriterLease(tmp_path).acquire(OTHER)
    assert ledger_writer.conflicts_total(tmp_path) == 2


def test_a_released_lease_lets_the_next_writer_in(tmp_path, lease):
    lease.release()
    lease.release()
    assert lease.held is False
    successor = ledger_writer.WriterLease(tmp_path).acquire(OTHER)
    try:
        assert successor.held is True
        assert ledger_writer.holder(tmp_path) == OTHER
    finally:
        successor.release()
    assert ledger_writer.conflicts_total(tmp_path) == 0


def test_a_new_holder_replaces_a_longer_record(tmp_path):
    (tmp_path / ledger_writer.LOCK).write_text(json.dumps({**CLAIMANT, "host": "a much longer host name " * 4}))
    held = ledger_writer.WriterLease(tmp_path).acquire({"pid": 1})
    try:
        assert ledger_writer.holder(tmp_path) == {"pid": 1}
    finally:
        held.release()


def test_the_lease_creates_its_folder(tmp_path):
    folder = tmp_path / "a" / "b"
    held = ledger_writer.WriterLease(folder).acquire(CLAIMANT)
    try:
        assert ledger_writer.holder(folder) == CLAIMANT
    finally:
        held.release()


def test_a_fresh_lease_is_not_held(tmp_path):
    assert ledger_writer.WriterLease(tmp_path).held is False
    assert ledger_writer.WriterLease(str(tmp_path)).directory == tmp_path


@pytest.mark.parametrize("content", [None, "not json", "[1, 2]"])
def test_an_unreadable_holder_record_reads_empty(tmp_path, content):
    if content is not None:
        (tmp_path / ledger_writer.LOCK).write_text(content)
    assert ledger_writer.holder(tmp_path) == {}


def test_a_folder_without_conflicts_counts_zero(tmp_path):
    assert ledger_writer.conflicts_total(tmp_path) == 0


def test_the_owner_names_this_process(monkeypatch):
    monkeypatch.setattr(ledger_writer.time, "time", lambda: 12.3456)
    assert ledger_writer.owner() == {"pid": os.getpid(), "host": socket.gethostname(), "started_at": 12345}


def test_a_snapshot_copies_committed_rows_still_in_the_write_ahead_log(tmp_path):
    live = database(tmp_path / "live.sqlite3", "first", "second")
    try:
        target = tmp_path / "backups" / "copy.sqlite3"
        assert ledger_writer.snapshot(tmp_path / "live.sqlite3", target) == target
    finally:
        live.close()
    assert texts(target) == ["first", "second"]
    assert sorted(path.name for path in target.parent.iterdir()) == ["copy.sqlite3"]
    copy = sqlite3.connect(target)
    try:
        assert copy.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    finally:
        copy.close()


def test_a_snapshot_accepts_text_paths(tmp_path):
    database(tmp_path / "live.sqlite3", "row").close()
    target = str(tmp_path / "copy.sqlite3")
    assert ledger_writer.snapshot(str(tmp_path / "live.sqlite3"), target) == Path(target)


def test_a_failed_snapshot_keeps_the_previous_copy_and_leaves_no_partial_file(tmp_path):
    target = tmp_path / "copy.sqlite3"
    database(target, "previous").close()
    with pytest.raises(sqlite3.OperationalError):
        ledger_writer.snapshot(tmp_path / "missing.sqlite3", target)
    assert texts(target) == ["previous"]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["copy.sqlite3"]


def test_an_unverified_snapshot_is_never_renamed_into_place(tmp_path, monkeypatch):
    database(tmp_path / "live.sqlite3", "row").close()
    monkeypatch.setattr(ledger_writer, "verify", Mock(side_effect=ValueError("bad copy")))
    with pytest.raises(ValueError, match="^bad copy$"):
        ledger_writer.snapshot(tmp_path / "live.sqlite3", tmp_path / "out" / "copy.sqlite3")
    assert list((tmp_path / "out").iterdir()) == []


def test_verify_names_a_failed_integrity_check(tmp_path, monkeypatch):
    connection = Mock()
    connection.execute.return_value.fetchone.return_value = ("row 3 missing from index",)
    connect = Mock(return_value=connection)
    monkeypatch.setattr(ledger_writer.sqlite3, "connect", connect)
    with pytest.raises(ValueError) as failed:
        ledger_writer.verify(tmp_path / "copy.sqlite3")
    assert str(failed.value) == f"{tmp_path / 'copy.sqlite3'} failed its integrity check: row 3 missing from index"
    connect.assert_called_once_with(f"{(tmp_path / 'copy.sqlite3').as_uri()}?mode=ro", uri=True)
    connection.execute.assert_called_once_with("PRAGMA integrity_check")
    connection.close.assert_called_once_with()


def test_verify_never_creates_a_missing_file(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        ledger_writer.verify(tmp_path / "missing.sqlite3")
    assert not (tmp_path / "missing.sqlite3").exists()


def test_a_restore_writes_the_backup_over_the_live_database(tmp_path):
    folder = tmp_path / "ledgers"
    folder.mkdir()
    database(folder / ledger_writer.DATABASE, "kept").close()
    ledger_writer.snapshot(folder / ledger_writer.DATABASE, tmp_path / "backup.sqlite3")
    database(folder / ledger_writer.DATABASE, "after the backup").close()
    assert ledger_writer.restore(tmp_path / "backup.sqlite3", folder) == folder / ledger_writer.DATABASE
    assert texts(folder / ledger_writer.DATABASE) == ["kept"]
    successor = ledger_writer.WriterLease(folder).acquire(OTHER)
    successor.release()


def test_a_restore_is_refused_while_a_writer_holds_the_folder(tmp_path, lease):
    database(tmp_path / ledger_writer.DATABASE, "live").close()
    ledger_writer.snapshot(tmp_path / ledger_writer.DATABASE, tmp_path / "backup" / "copy.sqlite3")
    database(tmp_path / ledger_writer.DATABASE, "newer").close()
    with pytest.raises(ledger_writer.WriterConflict):
        ledger_writer.restore(tmp_path / "backup" / "copy.sqlite3", tmp_path)
    assert texts(tmp_path / ledger_writer.DATABASE) == ["live", "newer"]


def test_a_missing_backup_is_refused_before_the_lease_is_taken(tmp_path, lease):
    with pytest.raises(sqlite3.OperationalError):
        ledger_writer.restore(tmp_path / "missing.sqlite3", tmp_path)
    assert ledger_writer.conflicts_total(tmp_path) == 0


def test_the_command_snapshots_and_restores_the_folder_database(tmp_path, capsys):
    database(tmp_path / ledger_writer.DATABASE, "row").close()
    assert ledger_writer.main(["--dir", str(tmp_path), "snapshot", str(tmp_path / "copy.sqlite3")]) == 0
    assert capsys.readouterr().out == f"{tmp_path / 'copy.sqlite3'}\n"
    assert ledger_writer.main(["--dir", str(tmp_path), "restore", str(tmp_path / "copy.sqlite3")]) == 0
    assert capsys.readouterr().out == f"{tmp_path / ledger_writer.DATABASE}\n"
    assert texts(tmp_path / ledger_writer.DATABASE) == ["row"]


def test_the_command_reports_the_holder_and_conflicts(tmp_path, lease, capsys, monkeypatch):
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path))
    with pytest.raises(ledger_writer.WriterConflict):
        ledger_writer.WriterLease(tmp_path).acquire(OTHER)
    assert ledger_writer.main(["holder"]) == 0
    assert json.loads(capsys.readouterr().out) == {"holder": CLAIMANT, "conflicts": 1}


def test_the_command_expands_the_home_folder(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert ledger_writer.main(["--dir", "~", "holder"]) == 0
    assert json.loads(capsys.readouterr().out) == {"holder": {}, "conflicts": 0}


def test_the_command_reports_a_refused_restore(tmp_path, lease, capsys):
    database(tmp_path / ledger_writer.DATABASE, "row").close()
    ledger_writer.snapshot(tmp_path / ledger_writer.DATABASE, tmp_path / "copy" / "c.sqlite3")
    assert ledger_writer.main(["--dir", str(tmp_path), "restore", str(tmp_path / "copy" / "c.sqlite3")]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == (
        f"ledger folder {tmp_path} already has an active writer, pid 4242 on writer-a; a second writer is refused\n"
    )


@pytest.mark.parametrize("error", [ValueError("bad copy"), sqlite3.DatabaseError("not a database")])
def test_the_command_reports_a_bad_backup(tmp_path, capsys, monkeypatch, error):
    monkeypatch.setattr(ledger_writer, "restore", Mock(side_effect=error))
    assert ledger_writer.main(["--dir", str(tmp_path), "restore", "copy.sqlite3"]) == 1
    assert capsys.readouterr().err == f"{error}\n"


def test_the_command_defaults_to_the_ledger_folder_setting(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path))
    database(tmp_path / ledger_writer.DATABASE, "row").close()
    assert ledger_writer.main(["snapshot", str(tmp_path / "copy.sqlite3")]) == 0
    assert texts(tmp_path / "copy.sqlite3") == ["row"]


def test_the_command_reads_the_development_ledger_folder_by_default(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("LEDGER_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    held = ledger_writer.WriterLease(tmp_path / "development-ledger").acquire(CLAIMANT)
    try:
        assert ledger_writer.main(["holder"]) == 0
    finally:
        held.release()
    assert json.loads(capsys.readouterr().out) == {"holder": CLAIMANT, "conflicts": 0}


def test_the_command_needs_a_subcommand(capsys):
    with pytest.raises(SystemExit) as stopped:
        ledger_writer.main([])
    assert stopped.value.code == 2


@pytest.fixture
def serving(monkeypatch, tmp_path):
    monkeypatch.setattr(server.core, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(server, "PIDFILE", tmp_path / ".server.pid")
    monkeypatch.setattr(server, "ThreadingHTTPServer", Mock())
    monkeypatch.setattr(server.threading, "Thread", Mock())
    monkeypatch.setattr(server.legacy, "adopt", Mock())
    return tmp_path


def test_a_second_server_on_a_held_folder_exits_before_touching_it(serving, lease):
    with pytest.raises(SystemExit) as stopped:
        server.serve()
    assert stopped.value.code == (
        f"ledger folder {serving} already has an active writer, pid 4242 on writer-a; a second writer is refused"
    )
    server.legacy.adopt.assert_not_called()
    server.ThreadingHTTPServer.assert_not_called()
    assert not (serving / ".server.pid").exists()


def test_the_server_holds_the_lease_while_it_serves_and_releases_it_after(serving):
    seen = []

    def serve_forever():
        seen.append(ledger_writer.holder(serving)["pid"])
        with pytest.raises(ledger_writer.WriterConflict):
            ledger_writer.WriterLease(serving).acquire(OTHER)

    server.ThreadingHTTPServer.return_value.serve_forever.side_effect = serve_forever
    server.serve()
    assert seen == [os.getpid()]
    server.legacy.adopt.assert_called_once_with(server.stored)
    successor = ledger_writer.WriterLease(serving).acquire(OTHER)
    successor.release()


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case, tmp_path):
    from tests.sv2_ldg04_cases import run_case

    first, second = run_case(case, tmp_path / "first"), run_case(case, tmp_path / "second")
    assert first == second
    committed = json.loads((EVIDENCE / f"{case}-result.json").read_text())
    assert committed == {"case": f"T-SV2-LDG-04-{case.upper()}", "independent_runs": 2, "observed": first}, json.dumps(
        first, indent=2, sort_keys=True
    )
