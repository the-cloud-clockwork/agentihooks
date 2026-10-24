import pytest

from scripts.swarm_ledger.repository import sqlite
from tests.swarm_ledger.test_sqlite import CONTENT, chat, store


@pytest.fixture
def pair(tmp_path, monkeypatch):
    writer = store(tmp_path)
    writer.create("ledger", CONTENT)
    writer.create("other", CONTENT)
    monkeypatch.setattr(writer.domain, "LEDGER_DIR", writer.directory)
    reader = sqlite.SQLiteLedgerRepository()
    return writer, reader


def test_generations_name_every_cached_ledger(pair):
    writer, _ = pair
    writer.get_document("ledger")
    writer.get_document("other")
    assert writer.generations() == {
        "ledger": writer.current("ledger").generation,
        "other": writer.current("other").generation,
    }


def test_a_carry_holds_only_the_ledgers_written_since(pair):
    writer, _ = pair
    writer.get_document("ledger")
    writer.get_document("other")
    since = writer.generations()
    writer.apply_ops("ledger", ops=[chat(1)])
    entry = writer.current("ledger")
    assert writer.carry(since) == {"ledger": [entry.generation, entry.text]}
    assert writer.carry(writer.generations()) == {}


def test_an_adopted_carry_serves_the_writes_without_reading_rows(pair, monkeypatch):
    writer, reader = pair
    reader.get_document("ledger")
    since = writer.generations()
    writer.apply_ops("ledger", ops=[chat(1)])
    carried = writer.carry(since)

    def refused(connection, slug):
        raise AssertionError(f"{slug} was read from rows")

    reader.adopt_carried(carried)
    monkeypatch.setattr(sqlite, "read_rows", refused)
    assert reader.get_document("ledger") == writer.get_document("ledger")
    assert reader.current("ledger").generation == carried["ledger"][0]


def test_a_carry_of_a_ledger_the_reader_never_loaded_is_adopted(pair, monkeypatch):
    writer, reader = pair
    writer.get_document("ledger")
    carried = writer.carry({})

    def refused(connection, slug):
        raise AssertionError(f"{slug} was read from rows")

    reader.adopt_carried(carried)
    monkeypatch.setattr(sqlite, "read_rows", refused)
    assert reader.current("ledger").generation == carried["ledger"][0]
    assert reader.current("ledger").state == writer.current("ledger").state


def test_an_older_carry_never_replaces_a_newer_copy(pair):
    writer, reader = pair
    writer.get_document("ledger")
    since = writer.generations()
    writer.apply_ops("ledger", ops=[chat(1)])
    old = writer.carry(since)
    writer.apply_ops("ledger", ops=[chat(2)])
    current = reader.current("ledger")
    reader.adopt_carried(old)
    assert reader.current("ledger") is current


def test_a_carry_the_reader_already_holds_is_not_parsed_again(pair, monkeypatch):
    writer, reader = pair
    writer.get_document("ledger")
    since = writer.generations()
    writer.apply_ops("ledger", ops=[chat(1)])
    held = reader.current("ledger")

    def refused(state):
        raise AssertionError("parsed a carry already held")

    monkeypatch.setattr(sqlite, "document", refused)
    reader.adopt_carried(writer.carry(since))
    assert reader.current("ledger") is held
