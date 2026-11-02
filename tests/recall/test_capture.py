"""Recall capture: ledger publishes, the tick step and swarm removal copy history into the archive."""

import argparse
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from scripts.recall import capture
from scripts.recall.models import RecallRecord
from scripts.recall.store import RecallStore, SQLiteRecallStore, SyncCounts
from scripts.swarm.store import RedisStore
from scripts.swarm_ledger.events.publishing import Publishing
from scripts.swarm_ledger.repository.entry import Entry

HANDOFF = (
    "## Intent\nBuild recall\n## Done\nTests passed\n## Stopped at\nReview\n"
    "## Decisions and promises\nKeep records\n## Next\nMerge\n## Read first\nIssue\n<!-- handoff complete -->"
)


def chat(numbers: range) -> list[dict[str, Any]]:
    """Chat entries whose text carries one searchable word per number."""
    return [{"id": f"m{n}", "by": "operator", "at": n, "text": f"tokenq{n}z"} for n in numbers]


class FakeRepository:
    """A ledger repository holding one document per slug, bumping its generation on every write."""

    def __init__(self) -> None:
        """Start with no ledgers."""
        self.documents: dict[str, dict[str, Any]] = {}
        self.generations: dict[str, int] = {}

    def current(self, slug: str) -> Entry:
        """The stored generation of ``slug``."""
        return Entry(self.generations.get(slug, 0), "", self.documents.get(slug, {"title": slug, "chat": []}))

    def get_document(self, slug: str) -> dict[str, Any]:
        """A copy of the stored document."""
        return self.current(slug).mutable()

    def apply_ops(
        self, slug: str, changes: list[Any] | None = None, ops: list[Any] | None = None, gate: Any = None, **_: Any
    ) -> tuple[dict[str, Any], list[Any]]:
        """Append each op's chat entries and store the next generation."""
        document = self.get_document(slug)
        for op in ops or []:
            document["chat"].extend(op["chat"])
        self.documents[slug] = document
        self.generations[slug] = self.generations.get(slug, 0) + 1
        return document, []


class Gated:
    """An archive opener that waits for its gate before handing out the store."""

    def __init__(self, path: Path) -> None:
        """Hold the archive path behind a closed gate."""
        self.path = path
        self.gate = threading.Event()

    def __call__(self) -> RecallStore:
        """Open the archive once the gate opens."""
        assert self.gate.wait(30)
        return SQLiteRecallStore(self.path)


def refusing() -> RecallStore:
    """An archive opener whose archive refuses every write."""
    raise OSError("disk refused")


class Counting:
    """An archive that counts its writes and passes them to a real one."""

    def __init__(self, path: Path) -> None:
        """Wrap the archive at ``path``."""
        self.inner = SQLiteRecallStore(path)
        self.calls: list[list[RecallRecord]] = []

    def sync(self, source: str, records: Any, deleted: Any = ()) -> SyncCounts:
        """Record the write and store it."""
        records = list(records)
        self.calls.append(records)
        return self.inner.sync(source, records, deleted)


@pytest.fixture
def source() -> RedisStore:
    """A swarm store on a fake Redis holding one configured swarm with one transfer."""
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.redis.hset(
        store.key("sample", "config"),
        mapping={"slug": "sample", "repo": "/repo", "max_eng": 1, "max_ci": 1, "state": "stopped"},
    )
    row = {
        "id": "transfer-one",
        "seat": "eng-1@sample",
        "task": "task-one",
        "predecessor": "old-engineer",
        "successor": "new-engineer",
        "at": 1234,
        "handoff": HANDOFF,
    }
    store.redis.hset(store.key("sample", "transfers"), row["id"], json.dumps(row))
    return store


def test_a_ledger_write_returns_before_the_archive_opens_and_its_chat_becomes_searchable(tmp_path: Path) -> None:
    inner = FakeRepository()
    opener = Gated(tmp_path / "recall.sqlite3")
    recall = capture.LedgerCapture(inner.get_document, opener, lambda: tmp_path)
    wrapper = Publishing(inner, recall)
    state, rejected = wrapper.apply_ops("alpha", ops=[{"chat": chat(range(1))}])
    assert (state["chat"][0]["text"], rejected) == ("tokenq0z", [])
    wrapper.drain(30)
    assert not opener.gate.is_set()
    assert not (tmp_path / "recall.sqlite3").exists()
    opener.gate.set()
    recall.drain(30)
    assert SQLiteRecallStore(tmp_path / "recall.sqlite3").match('"tokenq0z"') != []


def test_chat_past_the_retention_limit_stays_searchable(tmp_path: Path) -> None:
    documents = {"alpha": {"title": "Alpha", "chat": chat(range(510))}}
    path = tmp_path / "recall.sqlite3"
    recall = capture.LedgerCapture(lambda slug: documents[slug], lambda: SQLiteRecallStore(path), lambda: tmp_path)
    recall.index("alpha")
    documents["alpha"]["chat"] = documents["alpha"]["chat"][-500:]
    recall.index("alpha")
    archive = SQLiteRecallStore(path)
    assert len(archive.match('"tokenq0z"')) == 1
    assert len(archive.match('"tokenq509z"')) == 1


def test_a_refusing_archive_is_logged_and_leaves_the_write_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    inner = FakeRepository()
    recall = capture.LedgerCapture(inner.get_document, refusing, lambda: tmp_path)
    wrapper = Publishing(inner, recall)
    state, rejected = wrapper.apply_ops("alpha", ops=[{"chat": chat(range(2))}])
    wrapper.drain(30)
    recall.drain(30)
    assert ([m["id"] for m in state["chat"]], rejected) == (["m0", "m1"], [])
    assert inner.generations["alpha"] == 1
    assert "recall capture for ledger alpha: disk refused" in capsys.readouterr().err


def test_the_tick_step_writes_only_when_seat_data_changed(
    source: RedisStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.inbox.seats import SeatMemory, SeatRegistry

    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    archive = Counting(tmp_path / "recall.sqlite3")
    monkeypatch.setattr(capture, "archive", lambda: archive)
    SeatRegistry(source.redis).occupy("eng-1@sample", "new-engineer", 5)
    first = capture.swarm_pass("sample", source, {})
    assert len(archive.calls) == 1
    assert first == [f"recall archived {len(archive.calls[0])} swarm records"]
    assert capture.swarm_pass("sample", source, {}) == []
    assert len(archive.calls) == 1
    SeatMemory(source.redis).learn("eng-1@sample", "new-engineer", "Archive first because removal deletes", 6)
    assert capture.swarm_pass("sample", source, {}) == ["recall archived 1 swarm records"]
    assert len(archive.calls) == 2


def test_the_tick_step_reports_a_refused_archive_and_retries_next_pass(
    source: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(capture, "archive", refusing)
    assert capture.swarm_pass("sample", source, {}) == [
        "skipped recall capture: the archive refused its write: disk refused"
    ]
    assert source.redis.get(source.key("sample", capture.CURSOR)) is None


def test_swarm_removal_archives_transfers_before_deleting_their_keys(
    source: RedisStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm import cli

    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    monkeypatch.setattr(cli.activity, "default_root", lambda: tmp_path)
    seen: list[bool] = []
    real = capture.capture_swarm

    def watched(store: RedisStore, slug: str) -> SyncCounts:
        seen.append(bool(store.redis.hlen(store.key(slug, "transfers"))))
        return real(store, slug)

    monkeypatch.setattr(capture, "capture_swarm", watched)
    cli.cmd_remove(source, argparse.Namespace(slug="sample"))
    assert seen == [True]
    assert source.redis.keys(source.key("sample", "*")) == []
    archive = SQLiteRecallStore(tmp_path / "recall" / "recall.sqlite3")
    assert any("transfers/transfer-one" in key for key in archive.match('"Build"'))


def test_a_failed_capture_keeps_the_swarm(source: RedisStore) -> None:
    def refuse(slug: str) -> None:
        raise OSError("disk refused")

    with pytest.raises(OSError, match="disk refused"):
        source.remove("sample", refuse)
    assert source.redis.hlen(source.key("sample", "transfers")) == 1
