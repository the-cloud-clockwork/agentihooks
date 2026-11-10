"""The knowledge steps of the tick: keep each running swarm's knowledge index level with its ledger."""

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from scripts.ledger_knowledge import embedders, service
from scripts.ledger_knowledge.index import HASH, HashEmbedder, KnowledgeIndex
from scripts.tick import registry
from scripts.tick.step import Lane
from scripts.tick.steps import CallStep, pass_domains

_GUARD = threading.Lock()
_RUNNING: set[Path] = set()


def _claim(path: Path) -> bool:
    """Mark the index at path as refreshing and return True, or False while an earlier refresh of it still runs."""
    with _GUARD:
        if path in _RUNNING:
            return False
        _RUNNING.add(path)
        return True


def _release(path: Path) -> None:
    """Mark the index at path as no longer refreshing."""
    with _GUARD:
        _RUNNING.discard(path)


def _knowledge_warm(tick: Any, get: Callable[[str], Any]) -> list[str]:
    """Embed the new and changed items of the swarm's ledger into its knowledge index, so an ask finds it fresh."""
    slug: str = get("slug")
    if os.environ.get(embedders.PREFIX + "EMBEDDER", HASH) != HASH:
        return []
    path = service.index_path(slug, os.environ)
    if not _claim(path):
        return [f"knowledge warm of {slug} skipped: an earlier refresh is still running"]
    try:
        document: dict[str, Any] = get("tools").ledger.export(deadline=get("deadline"))
        counts = service.refresh(KnowledgeIndex(path, HashEmbedder()), document, int(time.time() * 1000))
    finally:
        _release(path)
    if not (counts.added or counts.changed or counts.removed):
        return []
    return [f"knowledge warm of {slug}: {counts.added} added, {counts.changed} changed, {counts.removed} removed"]


registry.register(
    CallStep(
        "knowledge warm",
        "wrapper metrics",
        _knowledge_warm,
        needs=frozenset(),
        reads=pass_domains("slug"),
        writes=frozenset(),
        lane=Lane("slow"),
        effects=frozenset({"ledger"}),
        feature="tick-knowledge-warm",
    )
)
