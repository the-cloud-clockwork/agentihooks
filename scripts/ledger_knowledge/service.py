"""The one query tool over a ledger's knowledge index: what the ledger is and how it is evolving."""

import os
from collections.abc import Callable, Mapping
from dataclasses import asdict
from pathlib import Path

from scripts.recall.store import agentihooks_home

from .chunks import ROOT, Document, chunk_ledger
from .index import Hit, KnowledgeIndex, Refresh

REFRESH_SECONDS = 120
CHANGES = 10


def index_path(slug: str, environ: Mapping[str, str] = os.environ) -> Path:
    """The index file of one ledger under the agentihooks home."""
    return agentihooks_home(environ) / "knowledge" / f"{slug}.sqlite3"


def refresh(index: KnowledgeIndex, document: Document, now: int) -> Refresh:
    """Bring the index level with the ledger document."""
    return index.refresh(chunk_ledger(document), now)


def stale(index: KnowledgeIndex, now: int) -> bool:
    """True once the last refresh is at least the refresh window old."""
    return now - index.refreshed_at() >= REFRESH_SECONDS * 1000


def _hit(hit: Hit) -> Document:
    return {"score": round(hit.score, 4), **asdict(hit.chunk), "parents": [asdict(parent) for parent in hit.parents]}


def ask(index: KnowledgeIndex, read: Callable[[], Document], question: str, now: int, limit: int = 5) -> Document:
    """Refresh a stale index from read, then answer with the ledger summary, recent changes and hits."""
    if stale(index, now):
        refresh(index, read(), now)
    root = index.get(f"{ROOT}#0")
    return {
        "ledger": {"title": root.title if root else "", "counts": index.counts()},
        "refreshed_at": index.refreshed_at(),
        "evolving": [{"at": at, **asdict(chunk)} for at, chunk in index.changes(CHANGES)],
        "hits": [_hit(hit) for hit in index.query(question, limit)],
    }
