"""Tests for the session catalog's Postgres adapter: statement translation, transactions, errors and selection."""

import sqlite3

import psycopg2
import pytest
from psycopg2 import extensions

from hooks.memory import catalog as catalog_module
from hooks.memory import index_worker, postgres
from hooks.memory.api import records

pytestmark = pytest.mark.unit


class FakeCursor:
    def __init__(self, raw):
        self.raw = raw

    def execute(self, statement, parameters=None):
        self.raw.calls.append((statement, parameters))
        if statement in self.raw.failures:
            raise self.raw.failures[statement]

    def fetchone(self):
        return self.raw.row


class FakeRaw:
    def __init__(self, row=(1,), failures=None, status=extensions.TRANSACTION_STATUS_IDLE):
        self.row, self.failures, self.status = row, failures or {}, status
        self.calls, self.closed, self.autocommit = [], False, False

    def cursor(self):
        return FakeCursor(self)

    def get_transaction_status(self):
        return self.status

    def close(self):
        self.closed = True


@pytest.mark.parametrize(
    "location,expected",
    [
        ("postgresql://u@h/db", True),
        ("postgres://u@h/db", True),
        ("/data/catalog.sqlite3", False),
        ("sqlite:///postgresql://", False),
        (None, False),
    ],
)
def test_selected_only_for_a_postgres_url(location, expected):
    assert postgres.selected(location) is expected


def test_selected_refuses_a_path_object(tmp_path):
    assert postgres.selected(tmp_path / "postgresql://x") is False


def test_pragmas_are_skipped():
    assert postgres.translate("PRAGMA journal_mode=WAL") is None


def test_placeholders_become_format_markers_and_percent_is_escaped():
    statement = "SELECT a FROM t WHERE b = ? AND c LIKE '50%' AND d = ?"
    assert postgres.translate(statement) == "SELECT a FROM t WHERE b = %s AND c LIKE '50%%' AND d = %s"


def test_a_self_increment_upsert_names_its_target_table():
    assert postgres.translate(catalog_module.COUNT) == (
        "INSERT INTO ingest_counter VALUES (%s, %s, %s) ON CONFLICT (access_domain, name) "
        "DO UPDATE SET value = ingest_counter.value + excluded.value"
    )


def test_a_self_increment_outside_an_insert_is_left_alone():
    statement = "UPDATE t SET value = value + excluded.value"
    assert postgres.translate(statement) == statement


def test_insert_or_ignore_becomes_on_conflict_do_nothing():
    assert postgres.translate(records.ISSUE) == (
        "INSERT INTO session_cursor VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING"
    )


def test_json_fields_and_lists_use_jsonb():
    statement = (
        "SELECT 1 FROM s WHERE json_extract(s.record, '$.content_hash') = x AND p IN (SELECT value FROM json_each(?))"
    )
    assert postgres.translate(statement) == (
        "SELECT 1 FROM s WHERE (s.record::jsonb ->> 'content_hash') = x AND p IN "
        '(SELECT value COLLATE "C" FROM jsonb_array_elements_text(%s::jsonb))'
    )


def test_tables_use_byte_order_text_wide_integers_doubles_and_a_pgvector_column():
    statement = (
        "CREATE TABLE IF NOT EXISTS t (a TEXT NOT NULL, n INTEGER NOT NULL, at REAL NOT NULL, vector TEXT NOT NULL)"
    )
    assert postgres.translate(statement) == (
        'CREATE TABLE IF NOT EXISTS t (a TEXT COLLATE "C" NOT NULL, n BIGINT NOT NULL, '
        "at DOUBLE PRECISION NOT NULL, vector vector NOT NULL)"
    )


def test_the_index_vector_table_stores_a_pgvector_column():
    assert "vector vector NOT NULL" in postgres.translate(index_worker.SCHEMA[3])


def test_column_types_change_only_in_table_definitions():
    statement = "UPDATE t SET a = ? WHERE kind = 'TEXT INTEGER REAL'"
    assert postgres.translate(statement) == "UPDATE t SET a = %s WHERE kind = 'TEXT INTEGER REAL'"


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT MAX(rowid) FROM session_event",
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'",
        "INSERT OR REPLACE INTO context_pack VALUES (?, ?)",
        "SELECT 1 WHERE EXISTS (SELECT 1 FROM json_each(cited) WHERE value = ?)",
    ],
)
def test_statements_without_a_translation_are_refused(statement):
    with pytest.raises(ValueError) as raised:
        postgres.translate(statement)
    assert str(raised.value) == postgres.UNSUPPORTED + statement[:80]


def test_execute_runs_the_translated_statement_with_tuple_parameters():
    raw = FakeRaw()
    cursor = postgres.Connection(raw).execute("SELECT a FROM t WHERE b = ?", ["x"])
    assert raw.calls == [("SELECT a FROM t WHERE b = %s", ("x",))]
    assert cursor.fetchone() == (1,)


def test_execute_without_parameters_passes_an_empty_tuple():
    raw = FakeRaw()
    postgres.Connection(raw).execute("SELECT 1")
    assert raw.calls == [("SELECT 1", ())]


def test_a_pragma_runs_nothing_and_still_returns_a_cursor():
    raw = FakeRaw()
    assert isinstance(postgres.Connection(raw).execute("PRAGMA busy_timeout=5000"), FakeCursor)
    assert raw.calls == []


def test_an_immediate_transaction_takes_the_writer_lock():
    raw = FakeRaw()
    postgres.Connection(raw).execute("BEGIN IMMEDIATE")
    assert raw.calls == [("BEGIN", None), ("SELECT pg_advisory_xact_lock(%s)", (postgres.WRITER_LOCK,))]


@pytest.mark.parametrize(
    "raised,expected",
    [
        (psycopg2.OperationalError("server closed the connection"), sqlite3.OperationalError),
        (psycopg2.IntegrityError("duplicate key"), sqlite3.IntegrityError),
    ],
)
def test_driver_errors_surface_as_the_catalog_error_classes(raised, expected):
    raw = FakeRaw(failures={"SELECT 1": raised})
    with pytest.raises(expected) as caught:
        postgres.Connection(raw).execute("SELECT 1")
    assert str(caught.value) == str(raised) and caught.value.__cause__ is raised


def test_other_driver_errors_pass_through():
    raised = psycopg2.ProgrammingError("syntax error")
    with pytest.raises(psycopg2.ProgrammingError):
        postgres.Connection(FakeRaw(failures={"SELECT 1": raised})).execute("SELECT 1")


@pytest.mark.parametrize(
    "status,expected",
    [
        (extensions.TRANSACTION_STATUS_IDLE, False),
        (extensions.TRANSACTION_STATUS_INTRANS, True),
        (extensions.TRANSACTION_STATUS_INERROR, True),
    ],
)
def test_in_transaction_follows_the_server_status(status, expected):
    assert postgres.Connection(FakeRaw(status=status)).in_transaction is expected


def test_close_closes_the_driver_connection():
    raw = FakeRaw()
    postgres.Connection(raw).close()
    assert raw.closed


def patched_connect(monkeypatch, raw):
    opened = []

    def connect(url):
        opened.append(url)
        return raw

    monkeypatch.setattr(psycopg2, "connect", connect)
    return opened


def test_connect_autocommits_and_verifies_pgvector(monkeypatch):
    raw = FakeRaw()
    opened = patched_connect(monkeypatch, raw)
    connection = postgres.connect("postgresql://u@h/db")
    assert opened == ["postgresql://u@h/db"] and connection.raw is raw and raw.autocommit is True
    assert raw.calls == [(postgres.VECTOR_INSTALLED, ())]
    assert not raw.closed


def test_connect_refuses_a_database_without_pgvector(monkeypatch):
    raw = FakeRaw(row=None)
    patched_connect(monkeypatch, raw)
    with pytest.raises(sqlite3.OperationalError) as raised:
        postgres.connect("postgresql://u@h/db")
    assert str(raised.value) == postgres.NO_VECTOR and raw.closed


def test_connect_closes_the_connection_when_the_extension_check_fails(monkeypatch):
    raw = FakeRaw(failures={postgres.VECTOR_INSTALLED: psycopg2.OperationalError("server closed the connection")})
    patched_connect(monkeypatch, raw)
    with pytest.raises(sqlite3.OperationalError) as raised:
        postgres.connect("postgresql://u@h/db")
    assert str(raised.value) == "server closed the connection" and raw.closed


def test_an_unreachable_server_is_a_catalog_operational_error(monkeypatch):
    def refuse(url):
        raise psycopg2.OperationalError("connection refused")

    monkeypatch.setattr(psycopg2, "connect", refuse)
    with pytest.raises(sqlite3.OperationalError) as raised:
        postgres.connect("postgresql://u@h/db")
    assert str(raised.value) == "connection refused"


def test_the_catalog_opens_postgres_for_a_postgres_url(monkeypatch):
    opened, memory = [], sqlite3.connect(":memory:", isolation_level=None)

    def connect(url):
        opened.append(url)
        return memory

    monkeypatch.setattr(postgres, "connect", connect)
    catalog = catalog_module.Catalog("postgresql://u@h/db")
    assert opened == ["postgresql://u@h/db"] and catalog.db is memory
    assert catalog.watermark("d", "s", "g") == 0


def test_the_catalog_stays_on_sqlite_for_a_path(monkeypatch, tmp_path):
    monkeypatch.setattr(postgres, "connect", lambda url: pytest.fail("Postgres was opened for a path"))
    catalog = catalog_module.Catalog(tmp_path / "nested/catalog.sqlite3")
    assert isinstance(catalog.db, sqlite3.Connection) and (tmp_path / "nested/catalog.sqlite3").exists()
