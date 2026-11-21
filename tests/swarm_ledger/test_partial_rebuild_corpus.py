"""Warm writes on the recorded operations corpus store, publish and relay byte for byte what a full rebuild does, and
spend less storage write CPU inside the ledger lock than it."""

import contextlib
import copy
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

pytestmark = [pytest.mark.xdist_group("ledger-corpus-replay"), pytest.mark.timeout(240, method="signal")]
PLANTED = "planted by the replay past every write method"
KEPT = 150
TRIMMED = ("tasks", "followups")
UNCOMPARED = frozenset({"ledgers", "outbox_sent"})
LEDGERS = "SELECT slug, revision, generation, summary, touched_at FROM ledgers"
Corpus = Callable[[], dict[str, Any]]


def digest(text: str) -> str:
    """The hex SHA-256 of ``text``."""
    return hashlib.sha256(text.encode()).hexdigest()


def trimmed() -> dict[str, Any]:
    """The recorded corpus with every recorded operation, on the recorded ledger cut to the tasks and followups the
    operations name plus the first KEPT others, their stamps, and the last KEPT events."""
    recorded = replay.corpus()
    named = json.dumps(recorded["ops"])
    state = dict(recorded["state"])
    for key in TRIMMED:
        state[key] = [item for index, item in enumerate(state[key]) if index < KEPT or str(item.get("id")) in named]
    kept = {f"{key}/{item.get('id')}" for key in TRIMMED for item in state[key]}
    meta = dict(state["_meta"])
    meta["stamps"] = {
        path: stamp
        for path, stamp in meta["stamps"].items()
        if path.split("/")[0] not in TRIMMED or "/".join(path.split("/")[:2]) in kept
    }
    meta["events"] = meta["events"][-KEPT:]
    state["_meta"] = meta
    return {**recorded, "state": state}


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


def recording(patch: pytest.MonkeyPatch, writes: list[dict[str, str]]) -> None:
    """Record after each request the stored text and writer copy of every write it made, outside the timed storage
    write."""
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
            pending.clear()

    patch.setattr(ledger_sqlite, "written", storing)
    patch.setattr(replay.mutations, "apply", applying)


def run(path: Path, corpus: Corpus, full: bool, plant: str | None = None) -> dict[str, Any]:
    """One replay of ``corpus`` on the warm write path, or forced onto the full rebuild, with what every write stored
    and the rows stored at the end."""
    writes: list[dict[str, str]] = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(replay, "corpus", corpus)
        numbered(patch)
        if full:
            patch.setattr(Entry, "for_write", full_copy)
        if plant is not None:
            planting(patch, plant)
        recording(patch, writes)
        report = replay.replay(path, patch)
    return {**report, "writes": writes, "rows": persisted(path / "ledger.sqlite3")}


@pytest.fixture(scope="module")
def recorded() -> dict[str, Any]:
    """The trimmed recorded corpus."""
    return trimmed()


@pytest.fixture(scope="module")
def replays(tmp_path_factory: pytest.TempPathFactory, recorded: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The corpus replayed on the warm write path first, then forced onto the full rebuild."""
    return {
        name: run(tmp_path_factory.mktemp(name), lambda: copy.deepcopy(recorded), full=name == "full")
        for name in ("partial", "full")
    }


@pytest.mark.feature("ledger-write-partial-rebuild")
def test_warm_writes_store_publish_and_relay_what_the_full_rebuild_does_on_the_recorded_corpus(
    replays: dict[str, dict[str, Any]], recorded: dict[str, Any]
) -> None:
    partial, full = replays["partial"], replays["full"]
    assert partial["requests"] == full["requests"] == len(recorded["ops"]) == len(replay.corpus()["ops"])
    assert [output["reply"] for output in partial["outputs"] if output["reply"].get("applied")]
    assert partial["outputs"] == full["outputs"]
    assert [write for write in partial["writes"] if "text" in write]
    assert partial["writes"] == full["writes"]
    assert partial["rows"] == full["rows"]


@pytest.mark.feature("ledger-write-partial-rebuild")
def test_a_write_into_an_untouched_writer_task_past_the_write_methods_turns_the_equivalence_red(
    tmp_path: Path, recorded: dict[str, Any]
) -> None:
    named = json.dumps(recorded["ops"])
    task = next(item["id"] for item in reversed(recorded["state"]["tasks"]) if item["id"] not in named)

    def corpus() -> dict[str, Any]:
        return {**copy.deepcopy(recorded), "ops": copy.deepcopy(recorded["ops"][:3])}

    partial = run(tmp_path / "partial", corpus, full=False, plant=task)
    full = run(tmp_path / "full", corpus, full=True, plant=task)
    assert [output["reply"] for output in full["outputs"]] == [output["reply"] for output in partial["outputs"]]
    assert full["writes"] != partial["writes"]
    assert full["outputs"] != partial["outputs"]
    assert full["rows"] != partial["rows"]


@pytest.mark.wall_clock
@pytest.mark.feature("ledger-write-partial-rebuild")
def test_warm_writes_spend_less_storage_write_cpu_than_the_full_rebuild_on_the_recorded_corpus(
    replays: dict[str, dict[str, Any]],
) -> None:
    partial, full = (replays[name]["phase_cpu_seconds_per_request"]["storage_write"] for name in ("partial", "full"))
    assert partial < full * 0.8
