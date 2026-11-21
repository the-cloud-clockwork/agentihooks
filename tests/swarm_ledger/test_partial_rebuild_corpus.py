"""Warm writes on the recorded operations corpus store, publish and relay byte for byte what a full rebuild does, and
spend less storage write CPU inside the ledger lock than it."""

import contextlib
import hashlib
import itertools
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from scripts.events import envelope
from scripts.swarm_ledger.repository import sqlite as ledger_sqlite
from scripts.swarm_ledger.repository.entry import Entry, thawed
from scripts.swarm_ledger.repository.fragments import Fragment, encoded
from tests.swarm_ledger import operations_cpu_replay as replay

PLANTED = "planted by the replay past every write method"
UNCOMPARED = frozenset({"ledgers", "outbox_sent"})
LEDGERS = "SELECT slug, revision, generation, summary, touched_at FROM ledgers"
Corpus = Callable[[], dict[str, Any]]


def digest(text: str) -> str:
    """The hex SHA-256 of ``text``."""
    return hashlib.sha256(text.encode()).hexdigest()


def persisted(path: Path) -> str:
    """A digest of every row the writes stored, leaving out the random ledger token and the relay send times."""
    with contextlib.closing(sqlite3.connect(path)) as connection:
        tables = [name for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        rows = {
            table: sorted(map(repr, connection.execute(f'SELECT * FROM "{table}"')))
            for table in sorted(tables)
            if table not in UNCOMPARED
        }
        rows["ledgers"] = sorted(map(repr, connection.execute(LEDGERS)))
    return digest(json.dumps(rows))


def numbered(patch: pytest.MonkeyPatch) -> None:
    """Number relay rows in write order with a fixed time, so two replays of the same writes store the same rows."""
    numbers = itertools.count()

    def create(**values: Any) -> envelope.Envelope:
        return envelope.Envelope(id=f"replay-{next(numbers)}", time=0, **values)

    patch.setattr(envelope, "create", create)


def full_copy(self: Entry) -> dict[str, Any]:
    """A fresh plain copy of the stored state for every write, sharing the stamp records as a cold write does, so the
    write serializes and compares every leaf again."""
    self.writer = None
    state = cast(dict[str, Any], self.state)
    meta = {key: value.copy() if key == "stamps" else thawed(value) for key, value in state["_meta"].items()}
    return {key: meta if key == "_meta" else thawed(value) for key, value in state.items()}


def planting(patch: pytest.MonkeyPatch, task: str) -> None:
    """Retitle ``task`` in every write's writer copy through ``dict.__setitem__``, which skips every write method."""
    for_write = Entry.for_write

    def planted(self: Entry) -> dict[str, Any]:
        writer = cast(dict[str, Any], for_write(self))
        target = next(item for item in writer["tasks"] if item["id"] == task)
        dict.__setitem__(target, "title", PLANTED)
        return writer

    patch.setattr(Entry, "for_write", planted)


def recording(patch: pytest.MonkeyPatch, path: Path, writes: list[dict[str, str]]) -> None:
    """Record after each request the stored text and writer copy of every write it made and the rows stored, outside
    the timed storage write."""
    written, apply = ledger_sqlite.written, replay.mutations.apply
    pending: list[tuple[Fragment, dict[str, Any]]] = []

    def storing(state: dict[str, Any], previous: Fragment | None) -> tuple[Fragment, dict[str, Any]]:
        fragment, writer = written(state, previous)
        pending.append((fragment, writer))
        return fragment, writer

    def applying(*args: Any, **kwargs: Any) -> Any:
        try:
            return apply(*args, **kwargs)
        finally:
            writes.extend({"text": digest(encoded(part)), "writer": replay.fingerprint(held)} for part, held in pending)
            writes.append({"rows": persisted(path / "ledger.sqlite3")})
            pending.clear()

    patch.setattr(ledger_sqlite, "written", storing)
    patch.setattr(replay.mutations, "apply", applying)


def run(path: Path, full: bool, plant: str | None = None, corpus: Corpus | None = None) -> dict[str, Any]:
    """One replay of the corpus on the warm write path, or forced onto the full rebuild, with what every write stored."""
    writes: list[dict[str, str]] = []
    with pytest.MonkeyPatch.context() as patch:
        if corpus is not None:
            patch.setattr(replay, "corpus", corpus)
        numbered(patch)
        if full:
            patch.setattr(Entry, "for_write", full_copy)
        if plant is not None:
            planting(patch, plant)
        recording(patch, path, writes)
        report = replay.replay(path, patch)
    return {**report, "writes": writes}


def prefix(count: int) -> Corpus:
    """The recorded corpus cut to its first ``count`` operations."""
    recorded = replay.corpus()
    return lambda: {**recorded, "ops": recorded["ops"][:count]}


def untouched_task() -> str:
    """The id of a recorded task no recorded operation names."""
    recorded = replay.corpus()
    named = json.dumps(recorded["ops"])
    return next(task["id"] for task in reversed(recorded["state"]["tasks"]) if task["id"] not in named)


@pytest.fixture(scope="module")
def replays(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, Any]]:
    """The corpus replayed on the warm write path first, then forced onto the full rebuild."""
    return {name: run(tmp_path_factory.mktemp(name), full=name == "full") for name in ("partial", "full")}


@pytest.mark.feature("ledger-write-partial-rebuild")
def test_warm_writes_store_publish_and_relay_what_the_full_rebuild_does_on_the_recorded_corpus(
    replays: dict[str, dict[str, Any]],
) -> None:
    partial, full = replays["partial"], replays["full"]
    assert partial["requests"] == full["requests"] == len(replay.corpus()["ops"])
    assert [output["reply"] for output in partial["outputs"] if output["reply"].get("applied")]
    assert partial["outputs"] == full["outputs"]
    assert [write for write in partial["writes"] if "text" in write]
    assert partial["writes"] == full["writes"]


@pytest.mark.feature("ledger-write-partial-rebuild")
def test_a_write_into_an_untouched_writer_task_past_the_write_methods_turns_the_equivalence_red(
    tmp_path: Path,
) -> None:
    task, corpus = untouched_task(), prefix(3)
    partial = run(tmp_path / "partial", full=False, plant=task, corpus=corpus)
    full = run(tmp_path / "full", full=True, plant=task, corpus=corpus)
    assert [output["reply"] for output in full["outputs"]] == [output["reply"] for output in partial["outputs"]]
    assert full["writes"] != partial["writes"]
    assert full["outputs"] != partial["outputs"]


@pytest.mark.wall_clock
@pytest.mark.feature("ledger-write-partial-rebuild")
def test_warm_writes_spend_less_storage_write_cpu_than_the_full_rebuild_on_the_recorded_corpus(
    replays: dict[str, dict[str, Any]],
) -> None:
    partial, full = (replays[name]["phase_cpu_seconds_per_request"]["storage_write"] for name in ("partial", "full"))
    assert partial < full * 0.8
