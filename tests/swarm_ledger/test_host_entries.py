"""Tests for host entries: a fresh repository starts from this host's copy of a ledger and refreshes it from the
recorded row changes instead of loading every row."""

import json
from pathlib import Path

import pytest

from scripts.swarm_ledger import storage
from scripts.swarm_ledger.repository import host_entries, postgres, sqlite
from scripts.swarm_ledger.repository.entry import Entry
from tests.swarm_ledger.test_sqlite import CONTENT, chat

ADDRESS = "postgresql://db.example/ledgers"
STATE = {"title": "Store", "_meta": {"rev": 1}}


def kept(tmp_path: Path) -> sqlite.SQLiteLedgerRepository:
    repository = sqlite.SQLiteLedgerRepository(tmp_path / "ledgers" / sqlite.DATABASE)
    repository.copies = host_entries.Copies(tmp_path / "home" / "entries", str(repository.path))
    return repository


def host_copy(tmp_path: Path) -> Path:
    [copy] = (tmp_path / "home" / "entries").iterdir()
    return copy


def held(tmp_path: Path, generation: int) -> Entry:
    copy = host_entries.read(host_copy(tmp_path), generation)
    assert copy is not None
    assert copy[0] == copy[1].generation
    return copy[1]


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
    adopted = fresh.current("ledger")
    assert adopted.generation == stored.generation
    assert fresh.current("ledger") is adopted
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
@pytest.mark.parametrize("text", [b"", b"not a generation\n{}", b"7\nnot json", b"7\n[]", b"7\nnull"])
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
    adopted = held(tmp_path, stored.generation)
    assert (adopted.generation, adopted.state) == (stored.generation, stored.state)
    assert host_copy(tmp_path).stat().st_mode & 0o777 == 0o600


@pytest.mark.feature("ledger-cold-write")
def test_a_warm_repository_rewrites_the_host_entry_once_it_refreshed_far_enough_past_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(host_entries, "BEHIND", 2)
    first = kept(tmp_path)
    first.create("ledger", CONTENT)
    first.current("ledger")
    other = sqlite.SQLiteLedgerRepository(first.path)
    other.apply_ops("ledger", ops=[chat(1)])
    other.apply_ops("ledger", ops=[chat(2)])
    refreshed = first.current("ledger")
    assert held(tmp_path, refreshed.generation).generation == refreshed.generation


def test_a_copy_that_cannot_replace_its_target_leaves_no_staged_file(tmp_path: Path) -> None:
    folder = tmp_path / "entries"
    (folder / "ledger.entry").mkdir(parents=True)
    host_entries.write(folder / "ledger.entry", 1, "{}")
    assert [file.name for file in folder.iterdir()] == ["ledger.entry"]


@pytest.mark.feature("ledger-cold-write")
def test_a_purged_ledger_keeps_no_host_entry(tmp_path: Path) -> None:
    first = kept(tmp_path)
    first.create("ledger", CONTENT)
    first.current("ledger")
    with first.transaction() as transaction:
        transaction.purge("ledger")
    assert list((tmp_path / "home" / "entries").iterdir()) == []


def test_dropping_a_ledger_with_no_host_copy_succeeds(tmp_path: Path) -> None:
    host_entries.Copies(tmp_path / "entries", ADDRESS).drop("ledger")
    assert not (tmp_path / "entries").exists()


def test_a_host_that_cannot_store_the_entry_keeps_no_copy(tmp_path: Path) -> None:
    folder = tmp_path / "entries"
    folder.mkdir()
    blocked = folder / "blocked"
    blocked.write_text("")
    host_entries.write(blocked / "ledger.entry", 1, "{}")
    assert [file.name for file in folder.iterdir()] == ["blocked"]


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
    assert held(tmp_path, refreshed.generation).generation == (refreshed.generation if rewritten else start)


@pytest.mark.feature("ledger-refresh-batched-reads")
def test_default_host_copy_catches_up_within_eight_generations(tmp_path: Path) -> None:
    first = kept(tmp_path)
    first.create("ledger", CONTENT)
    start = first.current("ledger").generation
    writer = sqlite.SQLiteLedgerRepository(first.path)
    for index in range(8):
        writer.apply_ops("ledger", ops=[chat(index)])
    fresh = kept(tmp_path).current("ledger")
    copy = held(tmp_path, fresh.generation)
    assert fresh.generation == start + 8
    assert copy.generation == fresh.generation
    assert copy.state == fresh.state
    assert kept(tmp_path).current("ledger").generation == copy.generation


def write_probe(repository: sqlite.SQLiteLedgerRepository, value: int, count: int) -> None:
    """Store count probe keys under the ledger's meta object, each holding value, at the next revision."""
    with repository.connect() as connection:
        entry = repository._entry(connection, "ledger")
    state = json.loads(entry.text)
    state["_meta"]["probe"] = {f"key{index}": value for index in range(count)}
    state["_meta"]["rev"] += 1
    with repository.connect() as connection, connection:
        repository._write(connection, "ledger", entry, state, [])


def stale_lookups(tmp_path: Path, count: int, monkeypatch: pytest.MonkeyPatch) -> int:
    """The point lookups a fresh repository makes refreshing its host copy after count keys of one object changed."""
    writer = sqlite.SQLiteLedgerRepository(tmp_path / "ledgers" / sqlite.DATABASE)
    writer.create("ledger", CONTENT)
    write_probe(writer, 0, count)
    kept(tmp_path).current("ledger")
    write_probe(writer, 1, count)
    cold = kept(tmp_path)
    queries: list[str] = []
    monkeypatch.setattr(cold, "trace", queries.append)
    assert cold.current("ledger").state["_meta"]["probe"] == {f"key{index}": 1 for index in range(count)}
    return len([query for query in queries if " AND path" in query])


@pytest.mark.feature("ledger-refresh-batched-reads")
def test_a_cold_reader_of_a_stale_host_copy_looks_up_changed_keys_in_a_fixed_number_of_queries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert stale_lookups(tmp_path / "few", 2, monkeypatch) == stale_lookups(tmp_path / "many", 400, monkeypatch)


def test_a_repository_without_host_copies_keeps_no_host_entry(tmp_path: Path, loads: list[str]) -> None:
    first = sqlite.SQLiteLedgerRepository(tmp_path / "ledgers" / sqlite.DATABASE)
    first.create("ledger", CONTENT)
    first.current("ledger")
    sqlite.SQLiteLedgerRepository(first.path).current("ledger")
    assert first.copies is None
    assert loads == ["ledger", "ledger"]


def test_a_refreshed_entry_never_kept_by_this_process_is_not_written(tmp_path: Path) -> None:
    copies = host_entries.Copies(tmp_path / "entries", ADDRESS)
    copies.keep("ledger", Entry(host_entries.BEHIND, json.dumps(STATE), STATE), False)
    assert not (tmp_path / "entries").exists()


def test_a_copy_is_adopted_only_by_its_own_database_and_ledger(tmp_path: Path) -> None:
    copies = host_entries.Copies(tmp_path, ADDRESS)
    copies.keep("ledger", Entry(3, json.dumps(STATE), STATE), True)
    adopted = copies.adopt("ledger", 3)
    assert adopted is not None
    assert (adopted.generation, adopted.mutable(), json.loads(adopted.text)) == (3, STATE, STATE)
    assert host_entries.Copies(tmp_path, "postgresql://other/ledgers").adopt("ledger", 3) is None
    assert copies.adopt("other", 3) is None


def test_a_host_entry_is_named_by_a_hash_of_its_database_address_and_slug(tmp_path: Path) -> None:
    named = host_entries.path(tmp_path, (ADDRESS, "ledger"))
    assert named.parent == tmp_path
    assert (len(named.stem), named.suffix) == (32, ".entry")
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
    assert built.copies is not None
    assert (built.copies.held, built.copies.database) == (tmp_path / "ledger-entries", ADDRESS)
    local = storage.build(tmp_path / sqlite.DATABASE)
    assert isinstance(local, sqlite.SQLiteLedgerRepository)
    assert local.copies is None


def test_a_bound_twin_shares_the_host_copies_of_its_repository(tmp_path: Path) -> None:
    copies = host_entries.Copies(tmp_path, ADDRESS)
    repository = postgres.PostgresLedgerRepository(ADDRESS, None, copies)
    twin = repository.bound(object())
    assert twin is not repository
    assert twin.copies is copies
