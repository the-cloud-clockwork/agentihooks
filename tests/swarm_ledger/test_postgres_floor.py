import sqlite3
from types import SimpleNamespace

import psycopg2
import pytest

from scripts.swarm_ledger import ledger_alerts
from scripts.swarm_ledger.repository import postgres, sqlite

ADDRESS = "postgresql://ledger@db.test/ledger"


class Answer:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class Statements:
    def __init__(self, answers):
        self.answers, self.log = answers, []

    def execute(self, sql, parameters=()):
        self.log.append((sql, parameters))
        return Answer(self.answers.get(sql, []))


class Domain:
    @staticmethod
    def now_ms():
        return 1234


def repository():
    return postgres.PostgresLedgerRepository(ADDRESS, domain=Domain)


def written(monkeypatch):
    calls = []

    def write(self, connection, slug, entry, state, events):
        calls.append((connection, slug, entry, state, events))
        return "entry"

    monkeypatch.setattr(sqlite.SQLiteLedgerRepository, "_write", write)
    return calls


def test_the_refusal_names_the_writer_version_and_the_required_one():
    assert postgres.refusal(0, 1) == (
        "ledger writer version 0 is below version 1, the oldest the shared database allows;"
        " update its code and restart it"
    )


def test_every_connection_carries_writer_version_one_and_the_database_requires_one():
    assert postgres.SESSION["options"].endswith(" -c agentihooks.ledger_writer_version=1")
    assert (postgres.VERSION, postgres.REQUIRED) == (1, 1)


def test_the_stored_version_only_rises_and_the_guard_refuses_unrecorded_bumps_below_it():
    assert "DO UPDATE SET version = GREATEST(writer_floor.version, EXCLUDED.version)" in postgres.RAISE_FLOOR
    assert "IF writer < required\n    AND NOT EXISTS (SELECT 1 FROM row_changes" in postgres.GUARD
    assert "slug = NEW.slug AND generation = NEW.generation) THEN" in postgres.GUARD
    assert f"ERRCODE = '{postgres.REFUSED_CODE}'" in postgres.GUARD
    assert "current_setting('agentihooks.ledger_writer_version', true), ''), '0')" in postgres.GUARD
    assert "pg_try_advisory_lock(1280788050, (writer * 65536 + required)::INTEGER)" in postgres.GUARD
    assert "WHEN (NEW.generation IS DISTINCT FROM OLD.generation)" in postgres.GUARD_TRIGGER
    assert "l.locktype = 'advisory' AND l.classid = ? AND l.objsubid = 2" in postgres.REFUSED_WRITERS
    assert "l.objid::BIGINT / 65536, l.objid::BIGINT % 65536" in postgres.REFUSED_WRITERS
    assert "COALESCE(host(a.client_addr), 'an unseen client')" in postgres.REFUSED_WRITERS
    assert "l.database = (SELECT oid FROM pg_database WHERE datname = current_database())" in postgres.REFUSED_WRITERS


def test_a_database_refusal_surfaces_as_refused_and_other_failures_stay_database_errors():
    class Floor(psycopg2.Error):
        pgcode = postgres.REFUSED_CODE
        diag = SimpleNamespace(message_primary="below")

    class Other(psycopg2.Error):
        pgcode = "XX000"

    with pytest.raises(postgres.Refused, match="^below$") as caught, postgres.mapped():
        raise Floor("context")
    assert isinstance(caught.value.__cause__, Floor)
    with pytest.raises(sqlite3.DatabaseError) as caught, postgres.mapped():
        raise Other("other")
    assert type(caught.value) is sqlite3.DatabaseError


def test_a_writer_below_the_stored_version_marks_its_session_refused_and_writes_nothing(monkeypatch):
    calls = written(monkeypatch)
    connection = Statements({postgres.FLOOR: [(postgres.VERSION + 1,)]})
    with pytest.raises(postgres.Refused) as refused:
        repository()._write(connection, "s", "old", {"_meta": {"rev": 3}}, [])
    assert str(refused.value) == postgres.refusal(postgres.VERSION, postgres.VERSION + 1)
    marked = (postgres.REFUSED_LOCK, postgres.VERSION * 65536 + postgres.VERSION + 1)
    assert connection.log == [(postgres.FLOOR, ()), (postgres.MARK_REFUSED, marked)]
    assert calls == []


@pytest.mark.parametrize("floor", [[], [(postgres.VERSION,)]])
def test_a_writer_at_or_above_the_stored_version_writes(monkeypatch, floor):
    calls = written(monkeypatch)
    connection = Statements({postgres.FLOOR: floor})
    state = {"_meta": {"rev": 3}}
    assert repository()._write(connection, "s", "old", state, ["e"]) == "entry"
    assert connection.log == [(postgres.FLOOR, ()), (postgres.REFUSED_WRITERS, (postgres.REFUSED_LOCK,))]
    assert calls == [(connection, "s", "old", {"_meta": {"rev": 3}}, ["e"])]


def test_each_refused_writer_becomes_one_open_master_alert_on_the_next_allowed_write(monkeypatch):
    calls = written(monkeypatch)
    refused = [("10.0.0.5", 0, 1), ("an unseen client", 1, 2)]
    connection = Statements({postgres.REFUSED_WRITERS: refused})
    state = {"_meta": {"rev": 7}, "alerts": [{"id": "al-1-0", "text": "kept", "state": "done"}]}
    repository()._write(connection, "s", "old", state, [])
    alerts = calls[0][3]["alerts"]
    texts = [
        f"The ledger writer at 10.0.0.5 was refused: {postgres.refusal(0, 1)}",
        f"The ledger writer at an unseen client was refused: {postgres.refusal(1, 2)}",
    ]
    assert alerts[1:] == [
        {"id": f"al-7-{n}", "text": text, "source": "writer", "target": "master", "state": "open", "at": 1234, "rev": 7}
        for n, text in enumerate(texts, 1)
    ]
    assert ledger_alerts.TARGETS[ledger_alerts.WRITER] == "master"


def test_a_refusal_alert_starts_the_alert_list_on_a_ledger_without_one(monkeypatch):
    calls = written(monkeypatch)
    connection = Statements({postgres.REFUSED_WRITERS: [("10.0.0.5", 0, 1)]})
    repository()._write(connection, "s", "old", {"_meta": {"rev": 2}}, [])
    assert [alert["text"] for alert in calls[0][3]["alerts"]] == [
        f"The ledger writer at 10.0.0.5 was refused: {postgres.refusal(0, 1)}"
    ]


def test_a_closed_refusal_alert_is_not_raised_again_while_its_writer_stays_connected(monkeypatch):
    calls = written(monkeypatch)
    text = f"The ledger writer at 10.0.0.5 was refused: {postgres.refusal(0, 1)}"
    connection = Statements({postgres.REFUSED_WRITERS: [("10.0.0.5", 0, 1)]})
    closed = {"id": "al-1-0", "text": text, "source": "writer", "state": "done"}
    repository()._write(connection, "s", "old", {"_meta": {"rev": 7}, "alerts": [closed]}, [])
    assert calls[0][3]["alerts"] == [closed]
