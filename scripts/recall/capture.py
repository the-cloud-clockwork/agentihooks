"""Capture ledger and swarm changes into the recall archive as they happen, ahead of retention and removal."""

import hashlib
import os
import sqlite3
import sys
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

from scripts.swarm.store import RedisStore

from .reindex import sync_ledger
from .store import RecallStore, SQLiteRecallStore, SyncCounts, agentihooks_home, content_hash, default_path
from .swarm import extract_swarm  # pyright: ignore[reportUnknownVariableType]

CURSOR = "recall-cursor"


def archive(environ: Mapping[str, str] = os.environ) -> RecallStore:
    """Open the recall archive the environment names."""
    return SQLiteRecallStore(default_path(environ))


class LedgerCapture:
    """A ledger publisher that queues each published slug and indexes the queued slugs on one background thread."""

    def __init__(
        self,
        read: Callable[[str], dict[str, Any]],
        store: Callable[[], RecallStore] = archive,
        home: Callable[[], Path] = agentihooks_home,
    ) -> None:
        """Keep the ledger reader, the archive opener and the agentihooks home the worker indexes with."""
        self.read = read
        self.store = store
        self.home = home
        self.queued = threading.Condition()
        self.pending: set[str] = set()
        self.busy = False
        self.worker: threading.Thread | None = None

    def __call__(self, slug: str, state: dict[str, Any]) -> None:
        """Queue ``slug`` for indexing and start the worker on first use; the published state is not read."""
        with self.queued:
            self.pending.add(slug)
            if self.worker is None:
                self.worker = threading.Thread(target=self.run, name="recall-capture", daemon=True)
                self.worker.start()
            self.queued.notify_all()

    def run(self) -> None:
        """Index every queued slug once per wake, so slugs queued while indexing coalesce into one later pass."""
        while True:
            with self.queued:
                self.queued.wait_for(lambda: self.pending)
                slugs, self.pending, self.busy = sorted(self.pending), set(), True
            for slug in slugs:
                self.index(slug)
            with self.queued:
                self.busy = False
                self.queued.notify_all()

    def index(self, slug: str) -> None:
        """Index one ledger, logging a failure without raising it."""
        try:
            sync_ledger(self.store(), slug, self.read(slug), self.home())
        except Exception as exc:
            sys.stderr.write(f"recall capture for ledger {slug}: {exc}\n")

    def drain(self, timeout: float) -> None:
        """Wait until no slug is queued or being indexed."""
        with self.queued:
            if not self.queued.wait_for(lambda: not self.pending and not self.busy, timeout):
                raise TimeoutError(f"recall capture still pending after {timeout} s")


def capture_swarm(store: RedisStore, slug: str, document: dict[str, Any] | None = None) -> SyncCounts:
    """Write the recall records of one swarm's seats, transfers, inbox and task folders to the archive."""
    return archive().sync(f"swarm/{slug}", extract_swarm(slug, store, document))


def swarm_pass(slug: str, store: RedisStore, document: dict[str, Any]) -> list[str]:
    """Index the swarm's seat data when it changed since the cursor, skipping the archive write otherwise."""
    records = extract_swarm(slug, store, document)
    digest = hashlib.sha256("\n".join(sorted(f"{r.key} {content_hash(r)}" for r in records)).encode()).hexdigest()
    held = cast(Any, store)
    key: str = held.key(slug, CURSOR)
    if held.redis.get(key) == digest:
        return []
    try:
        counts = archive().sync(f"swarm/{slug}", records)
    except (sqlite3.Error, OSError) as exc:
        return [f"skipped recall capture: the archive refused its write: {exc}"]
    held.redis.set(key, digest)
    return [f"recall archived {counts.written} swarm records"] if counts.written else []
