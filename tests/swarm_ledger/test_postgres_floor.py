import socket
import sqlite3
from contextlib import contextmanager
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

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


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


def test_every_connection_carries_this_writer_version():
    assert postgres.SESSION["options"].endswith(f" -c agentihooks.ledger_writer_version={postgres.VERSION}")
    assert postgres.VERSION >= postgres.REQUIRED == 1


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


def test_a_writer_below_the_stored_version_writes_nothing(monkeypatch):
    calls = written(monkeypatch)
    connection = Statements({postgres.FLOOR: [(postgres.VERSION + 1,)]})
    with pytest.raises(postgres.Refused) as refused:
        repository()._write(connection, "s", "old", {"_meta": {"rev": 3}}, [])
    assert str(refused.value) == postgres.refusal(postgres.VERSION, postgres.VERSION + 1)
    assert connection.log == [(postgres.FLOOR, ())]
    assert calls == []


@pytest.mark.parametrize("floor", [[], [(postgres.VERSION,)]])
def test_a_writer_at_or_above_the_stored_version_writes(monkeypatch, floor):
    calls = written(monkeypatch)
    connection = Statements({postgres.FLOOR: floor})
    state = {"_meta": {"rev": 3}}
    assert repository()._write(connection, "s", "old", state, ["e"]) == "entry"
    assert connection.log == [(postgres.FLOOR, ()), (postgres.TAKE_REFUSALS, ("s",))]
    assert calls == [(connection, "s", "old", {"_meta": {"rev": 3}}, ["e"])]


def test_recorded_refusals_become_one_open_master_alert_each_on_the_next_allowed_write(monkeypatch):
    calls = written(monkeypatch)
    connection = Statements({postgres.TAKE_REFUSALS: [("first",), ("second",)]})
    state = {"_meta": {"rev": 7}, "alerts": [{"id": "al-1-0", "text": "kept", "state": "done"}]}
    repository()._write(connection, "s", "old", state, [])
    alerts = calls[0][3]["alerts"]
    assert alerts[1:] == [
        {"id": f"al-7-{n}", "text": text, "source": "writer", "target": "master", "state": "open", "at": 1234, "rev": 7}
        for n, text in ((1, "first"), (2, "second"))
    ]
    assert ledger_alerts.TARGETS[ledger_alerts.WRITER] == "master"


def test_a_refused_write_records_its_refusal_for_the_ledger_and_raises(monkeypatch):
    def refuse(self, slug, *args, **kwargs):
        assert (slug, args, kwargs) == ("s", ("changes",), {"ops": ["op"]})
        raise postgres.Refused("below")

    connection = Statements({})
    opened = []

    @contextmanager
    def connect(self):
        opened.append(True)
        yield connection

    monkeypatch.setattr(sqlite.SQLiteLedgerRepository, "apply_ops", refuse)
    monkeypatch.setattr(postgres.PostgresLedgerRepository, "connect", connect)
    with pytest.raises(postgres.Refused, match="^below$"):
        repository().apply_ops("s", "changes", ops=["op"])
    text = f"The ledger writer on {socket.gethostname()} was refused: below"
    assert connection.log == [(postgres.RECORD_REFUSAL, ("s", text, 1234))]
    assert opened == [True]


def test_an_allowed_write_records_no_refusal(monkeypatch):
    monkeypatch.setattr(sqlite.SQLiteLedgerRepository, "apply_ops", lambda self, slug, *a, **k: ("state", []))

    def connect(self):
        raise AssertionError("an allowed write opened a second connection")

    monkeypatch.setattr(postgres.PostgresLedgerRepository, "connect", connect)
    assert repository().apply_ops("s", ops=[]) == ("state", [])
