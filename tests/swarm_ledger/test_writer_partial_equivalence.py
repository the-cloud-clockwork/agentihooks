"""Recorded writes store byte for byte what the full rebuild stores, while the partial rebuild holds the lock less."""

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from scripts.swarm_ledger.repository import fragments, sqlite, tracked, writer_copy
from scripts.swarm_ledger.repository.entry import Entry
from scripts.swarm_ledger.repository.fragments import Fragment
from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository
from tests.swarm_ledger import operations_cpu_replay as replay

pytestmark = pytest.mark.xdist_group("fakeredis")

PLANTED_WRITES = 3


def full_rebuild(document: Any) -> Any:
    """``fragments.document`` with every writer copy leaf compared again, the way a write rebuilt before tracking."""

    def rebuilt(state: dict[str, Any], previous: Fragment | None = None) -> Fragment:
        clean = tracked.clean
        tracked.clean = lambda value: None
        try:
            return document(state, previous)
        finally:
            tracked.clean = clean

    return rebuilt


def planted(for_write: Any) -> Any:
    """``Entry.for_write`` with the last task's title changed behind the writer copy's tracking."""

    def plant(entry: Entry) -> dict[str, Any]:
        writer = for_write(entry)
        dict.__setitem__(writer["tasks"][-1], "title", "planted")
        return writer

    return plant


def persisted(path: Path) -> dict[str, list[Any]]:
    """Every stored row of the replay database that a write decides, outbox events without their envelope ids."""
    with sqlite3.connect(path) as connection:
        tables = {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY 1, 2, 3").fetchall()
            for table in (*sqlite.TABLES, "events", "row_changes", "work_nodes", "work_dependencies")
        }
        rows = connection.execute("SELECT slug, revision, subject, value FROM outbox").fetchall()
    events = [(slug, revision, subject, json.loads(value)) for slug, revision, subject, value in rows]
    tables["outbox"] = sorted(
        (slug, revision, subject, event["type"], json.dumps(event["data"], sort_keys=True))
        for slug, revision, subject, event in events
    )
    return tables


def recorded(folder: Path, full: bool, plant: bool = False) -> dict[str, Any]:
    """The replay report, the stored text of every generation and the persisted rows of one recorded replay."""
    texts: dict[int, bytes] = {}
    remember = SQLiteLedgerRepository._remember

    def remembered(repository: SQLiteLedgerRepository, slug: str, entry: Entry) -> None:
        texts[entry.generation] = fragments.exported(entry.fragments) if entry.fragments else entry.text.encode()
        remember(repository, slug, entry)

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(SQLiteLedgerRepository, "_remember", remembered)
        patched.setattr(sqlite.secrets, "token_urlsafe", lambda size: "t" * size)
        if full:
            patched.setattr(writer_copy, "document", full_rebuild(writer_copy.document))
        if plant:
            corpus = replay.corpus()
            patched.setattr(replay, "corpus", lambda: {**corpus, "ops": corpus["ops"][:PLANTED_WRITES]})
            patched.setattr(Entry, "for_write", planted(Entry.for_write))
        report = replay.replay(folder, patched)
    return {"report": report, "texts": texts, "rows": persisted(folder / "ledger.sqlite3")}


def differences(left: dict[str, Any], right: dict[str, Any]) -> list[str]:
    """What two recorded replays stored, answered, streamed or relayed differently."""
    found = [
        f"write {index} {field}"
        for index, (one, other) in enumerate(zip(left["report"]["outputs"], right["report"]["outputs"], strict=True))
        for field in ("reply", "state", "stream", "relays", "relay_outcomes")
        if one[field] != other[field]
    ]
    found += [
        f"text of generation {key}" for key in sorted(left["texts"]) if left["texts"][key] != right["texts"].get(key)
    ]
    found += [f"stored {table}" for table in left["rows"] if left["rows"][table] != right["rows"][table]]
    return found + (["generations"] if left["texts"].keys() != right["texts"].keys() else [])


@pytest.fixture(scope="module")
def replays(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, Any]]:
    return {
        "partial": recorded(tmp_path_factory.mktemp("partial"), full=False),
        "full": recorded(tmp_path_factory.mktemp("full"), full=True),
    }


@pytest.mark.feature("ledger-write-partial-rebuild")
def test_recorded_writes_store_answer_stream_and_relay_what_the_full_rebuild_does(
    replays: dict[str, dict[str, Any]],
) -> None:
    partial, full = replays["partial"], replays["full"]
    assert len(partial["texts"]) > len(replay.corpus()["ops"])
    assert differences(partial, full) == []


@pytest.mark.feature("ledger-write-partial-rebuild")
def test_a_write_behind_the_tracking_of_one_untouched_task_turns_the_equivalence_red(tmp_path: Path) -> None:
    partial = recorded(tmp_path / "partial", full=False, plant=True)
    full = recorded(tmp_path / "full", full=True, plant=True)
    found = differences(partial, full)
    assert any(item.startswith("text of generation") for item in found)
    assert {f"stored {table}" for table in sqlite.TABLES} & set(found)
    assert b'"title":"planted"' in b"".join(full["texts"].values())
    assert b'"title":"planted"' not in b"".join(partial["texts"].values())


@pytest.mark.wall_clock
@pytest.mark.feature("ledger-write-partial-rebuild")
def test_the_partial_rebuild_spends_less_storage_write_time_inside_the_lock_on_the_recorded_writes(
    replays: dict[str, dict[str, Any]],
) -> None:
    spent = {mode: run["report"]["phase_cpu_seconds_per_request"]["storage_write"] for mode, run in replays.items()}
    assert spent["partial"] < 0.8 * spent["full"], spent
