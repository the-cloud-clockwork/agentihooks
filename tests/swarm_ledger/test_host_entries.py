"""Tests for host entries: a fresh repository starts from this host's copy of a ledger and refreshes it from the
recorded row changes instead of loading every row."""

import json
from pathlib import Path

import pytest

from scripts.swarm_ledger import storage
from scripts.swarm_ledger.repository import host_entries, postgres, sqlite
from tests.swarm_ledger.test_sqlite import CONTENT, chat

ADDRESS = "postgresql://db.example/ledgers"


def kept(tmp_path: Path) -> sqlite.SQLiteLedgerRepository:
    return sqlite.SQLiteLedgerRepository(tmp_path / "ledgers" / sqlite.DATABASE, entries=tmp_path / "entries")


def host_copy(tmp_path: Path) -> Path:
    [copy] = (tmp_path / "entries").iterdir()
    return copy


@pytest.fixture
def loads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    counted: list[str] = []
    original = sqlite.load

    def counting(connection: object, slug: str) -> object:
        counted.append(slug)
        return original(connection, slug)

    monkeypatch.setattr(sqlite, "load", counting)
    return counted


@pytest.mark.feature("ledger-cold-write")
def test_a_fresh_repository_writes_from_the_host_entry_without_a_whole_load(tmp_path: Path, loads: list[str]) -> None:
    first = kept(tmp_path)
    first.create("ledger", CONTENT)
    first.current("ledger")
    first.apply_ops("ledger", ops=[chat(1)])
    loads.clear()
    fresh = kept(tmp_path)
    fresh.current("ledger")
    fresh.apply_ops("ledger", ops=[chat(2)])
    assert loads == []
    assert fresh.current("ledger").state == sqlite.SQLiteLedgerRepository(first.path).current("ledger").state
    assert loads == ["ledger"]


@pytest.mark.feature("ledger-cold-write")
def test_a_host_entry_at_the_stored_generation_is_served_as_it_is(tmp_path: Path, loads: list[str]) -> None:
    first = kept(tmp_path)
    first.create("ledger", CONTENT)
    stored = first.current("ledger")
    loads.clear()
    fresh = kept(tmp_path)
    assert fresh.current("ledger").generation == stored.generation
    assert fresh.get_document("ledger") == first.get_document("ledger")
    assert loads == []


@pytest.mark.feature("ledger-cold-write")
def test_a_host_entry_newer_than_the_database_is_never_adopted(tmp_path: Path, loads: list[str]) -> None:
    first = kept(tmp_path)
    first.create("ledger", CONTENT)
    stored = first.current("ledger")
    host_entries.write(host_copy(tmp_path), stored.generation + 1, json.dumps({**stored.state, "title": "Forged"}))
    loads.clear()
    assert kept(tmp_path).current("ledger").state["title"] == "Store"
    assert loads == ["ledger"]


@pytest.mark.feature("ledger-cold-write")
@pytest.mark.parametrize("text", [b"", b"not a generation\n{}", b"7\nnot json"])
def test_an_unreadable_host_entry_falls_back_to_the_whole_load(tmp_path: Path, loads: list[str], text: bytes) -> None:
    first = kept(tmp_path)
    first.create("ledger", CONTENT)
    first.current("ledger")
    host_copy(tmp_path).write_bytes(text)
    loads.clear()
    assert kept(tmp_path).get_document("ledger") == first.get_document("ledger")
    assert loads == ["ledger"]


@pytest.mark.feature("ledger-cold-write")
def test_a_whole_load_writes_the_host_entry(tmp_path: Path) -> None:
    first = kept(tmp_path)
    first.create("ledger", CONTENT)
    stored = first.current("ledger")
    adopted = host_entries.read(host_copy(tmp_path), stored.generation)
    assert adopted is not None
    assert (adopted.generation, adopted.state) == (stored.generation, stored.state)


@pytest.mark.feature("ledger-cold-write")
@pytest.mark.parametrize(("behind", "rewritten"), [(2, True), (3, False)])
def test_the_host_entry_is_rewritten_only_once_the_adopted_copy_is_far_enough_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, behind: int, rewritten: bool
) -> None:
    monkeypatch.setattr(host_entries, "BEHIND", behind)
    first = kept(tmp_path)
    first.create("ledger", CONTENT)
    start = first.current("ledger").generation
    first.apply_ops("ledger", ops=[chat(1)])
    first.apply_ops("ledger", ops=[chat(2)])
    refreshed = kept(tmp_path).current("ledger")
    held = host_entries.read(host_copy(tmp_path), refreshed.generation)
    assert held is not None
    assert held.generation == (refreshed.generation if rewritten else start)


def test_a_repository_without_a_host_folder_keeps_no_host_entry(tmp_path: Path, loads: list[str]) -> None:
    first = sqlite.SQLiteLedgerRepository(tmp_path / "ledgers" / sqlite.DATABASE)
    first.create("ledger", CONTENT)
    first.current("ledger")
    sqlite.SQLiteLedgerRepository(first.path).current("ledger")
    assert loads == ["ledger", "ledger"]
    assert not (tmp_path / "entries").exists()


def test_a_host_entry_is_named_by_a_hash_of_its_database_address_and_slug(tmp_path: Path) -> None:
    named = host_entries.path(tmp_path, (ADDRESS, "ledger"))
    assert named.parent == tmp_path
    assert "db.example" not in named.name
    assert named != host_entries.path(tmp_path, (ADDRESS, "other"))
    assert named == host_entries.path(tmp_path, (ADDRESS, "ledger"))


def test_the_host_folder_is_under_the_agentihooks_home(tmp_path: Path) -> None:
    assert host_entries.folder({}) == Path.home() / ".agentihooks" / "ledger-entries"
    assert host_entries.folder({"AGENTIHOOKS_HOME": str(tmp_path)}) == tmp_path / "ledger-entries"


@pytest.mark.feature("ledger-cold-write")
def test_a_repository_over_the_shared_database_keeps_host_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage, "shared", lambda: ADDRESS)
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    built = storage.build()
    assert isinstance(built, postgres.PostgresLedgerRepository)
    assert built.entries == tmp_path / "ledger-entries"
    local = storage.build(tmp_path / sqlite.DATABASE)
    assert isinstance(local, sqlite.SQLiteLedgerRepository)
    assert local.entries is None


def test_a_bound_twin_keeps_the_host_folder_of_its_repository(tmp_path: Path) -> None:
    other = object()
    shared = postgres.PostgresLedgerRepository(ADDRESS, None, tmp_path)
    assert shared.bound(other).entries == tmp_path
    local = sqlite.SQLiteLedgerRepository(None, None, tmp_path)
    assert local.bound(other).entries == tmp_path
