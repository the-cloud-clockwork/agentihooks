"""Warm writes on the recorded operations corpus store, publish and relay byte for byte what a full rebuild does, and
spend less storage write CPU inside the ledger lock than it."""

import contextlib
import copy
import hashlib
import itertools
import json
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from scripts.events import envelope
from scripts.swarm_ledger.repository import sqlite as ledger_sqlite
from scripts.swarm_ledger.repository.entry import Entry, thawed
from scripts.swarm_ledger.repository.fragments import Fragment, encoded
from tests.swarm_ledger import operations_cpu_replay as replay

pytestmark = [
    pytest.mark.xdist_group("ledger-corpus-replay"),
    pytest.mark.timeout(240, method="signal"),
    pytest.mark.wall_clock,
]
PLANTED = "planted by the replay past every write method"
KEPT = 150
TRIMMED = ("tasks", "followups")
QUERIES = {
    "ledgers": "SELECT slug, revision, generation, summary, touched_at FROM ledgers",
    "outbox_sent": "SELECT id FROM outbox_sent",
}
Corpus = Callable[[], dict[str, Any]]
RERUN_PROBE = (
    "import itertools\n"
    "from pathlib import Path\n"
    "from tests.swarm_ledger import test_partial_rebuild_corpus as corpus\n"
    "from tests.swarm_ledger.test_partial_rebuild_corpus import full, partial, recorded\n"
    "from tests.swarm_ledger.test_partial_rebuild_corpus import (\n"
    "    test_warm_writes_spend_less_storage_write_cpu_than_the_full_rebuild_on_the_recorded_corpus,\n"
    ")\n"
    "CALLS = Path(__file__).with_name('calls.txt')\n"
    "PAIRS = itertools.count()\n"
    "def run(path, replayed, full, plant=None):\n"
    "    pair = next(PAIRS) // 2\n"
    "    with CALLS.open('a') as calls:\n"
    "        calls.write(f'{pair} {full}\\n')\n"
    "    seconds = 1.5 if full else 2.0 if pair == 0 else 1.0\n"
    "    return {'phase_cpu_seconds_per_request': {'storage_write': seconds}}\n"
    "corpus.run = run\n"
    "corpus.replay.corpus = dict\n"
)


def digest(text: str) -> str:
    """The hex SHA-256 of ``text``."""
    return hashlib.sha256(text.encode()).hexdigest()


def trimmed() -> dict[str, Any]:
    """The recorded corpus on the recorded ledger cut to the tasks and followups its operations name plus the first
    KEPT others, their stamps, and the last KEPT events."""
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


def persisted(path: Path) -> dict[str, list[str]]:
    """Every row the writes stored, table by table, leaving out the random ledger token and the relay send times."""
    with contextlib.closing(sqlite3.connect(path)) as connection:
        tables = [name for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {
            table: sorted(map(repr, connection.execute(QUERIES.get(table, f'SELECT * FROM "{table}"'))))
            for table in sorted(tables)
        }


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
    """The recorded corpus, read once."""
    return replay.corpus()


@pytest.fixture(scope="module")
def partial(tmp_path_factory: pytest.TempPathFactory, recorded: dict[str, Any]) -> dict[str, Any]:
    """The recorded corpus replayed on the warm write path."""
    return run(tmp_path_factory.mktemp("partial"), lambda: copy.deepcopy(recorded), full=False)


@pytest.fixture(scope="module")
def full(tmp_path_factory: pytest.TempPathFactory, recorded: dict[str, Any]) -> dict[str, Any]:
    """The recorded corpus replayed with every write forced onto the full rebuild."""
    return run(tmp_path_factory.mktemp("full"), lambda: copy.deepcopy(recorded), full=True)


def fresh_pair(path: Path, recorded: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The recorded corpus replayed again into ``path``, once on the warm write path and once on the full rebuild."""
    return (
        run(path / "partial", lambda: copy.deepcopy(recorded), full=False),
        run(path / "full", lambda: copy.deepcopy(recorded), full=True),
    )


@pytest.mark.feature("ledger-write-partial-rebuild")
def test_the_warm_write_replay_answers_every_recorded_write(partial: dict[str, Any], recorded: dict[str, Any]) -> None:
    assert partial["requests"] == len(partial["outputs"]) == len(recorded["ops"])
    assert [output for output in partial["outputs"] if output["reply"].get("applied")]
    assert [write for write in partial["writes"] if "text" in write]


@pytest.mark.feature("ledger-write-partial-rebuild")
def test_warm_writes_store_publish_and_relay_what_the_full_rebuild_does_on_the_recorded_corpus(
    partial: dict[str, Any], full: dict[str, Any]
) -> None:
    assert partial["outputs"] == full["outputs"]
    assert partial["writes"] == full["writes"]
    assert partial["rows"] == full["rows"]


@pytest.mark.feature("ledger-write-partial-rebuild")
def test_a_write_into_an_untouched_writer_task_past_the_write_methods_turns_the_equivalence_red(
    tmp_path: Path,
) -> None:
    cut = trimmed()
    named = json.dumps(cut["ops"])
    task = next(item["id"] for item in reversed(cut["state"]["tasks"]) if item["id"] not in named)

    def corpus() -> dict[str, Any]:
        return {**copy.deepcopy(cut), "ops": copy.deepcopy(cut["ops"][:3])}

    partial = run(tmp_path / "partial", corpus, full=False, plant=task)
    full = run(tmp_path / "full", corpus, full=True, plant=task)
    assert [output["reply"] for output in full["outputs"]] == [output["reply"] for output in partial["outputs"]]
    assert full["writes"] != partial["writes"]
    assert full["outputs"] != partial["outputs"]
    assert any(PLANTED in row for rows in full["rows"].values() for row in rows)
    assert not any(PLANTED in row for rows in partial["rows"].values() for row in rows)


@pytest.mark.feature("ledger-write-partial-rebuild")
@pytest.mark.flaky(reruns=1)
@pytest.mark.timeout(600, method="signal")
def test_warm_writes_spend_less_storage_write_cpu_than_the_full_rebuild_on_the_recorded_corpus(
    partial: dict[str, Any],
    full: dict[str, Any],
    recorded: dict[str, Any],
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> None:
    if request.node.execution_count > 1:
        partial, full = fresh_pair(tmp_path, recorded)
    storage = "storage_write"
    assert partial["phase_cpu_seconds_per_request"][storage] < full["phase_cpu_seconds_per_request"][storage]


@pytest.mark.feature("ledger-write-partial-rebuild")
def test_a_rerun_of_the_storage_write_cpu_test_measures_its_own_warm_and_full_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = tmp_path / "test_rerun_probe.py"
    probe.write_text(RERUN_PROBE)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    child = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "pytest_rerunfailures", "--reruns", "1", str(probe)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert "1 rerun" in child.stdout, child.stdout + child.stderr
    assert (tmp_path / "calls.txt").read_text().splitlines() == ["0 False", "0 True", "1 False", "1 True"], child.stdout
    assert child.returncode == 0, child.stdout
