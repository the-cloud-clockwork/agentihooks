"""Ledger interchange: lossless JSON export and import, and the one time import of pre SQLite ledger files."""

import json
from pathlib import Path

from scripts.swarm_ledger.repository import legacy, repository

ESCAPE_NON_ASCII = False


def export(slug: str, out: Path | None = None) -> dict:
    """The complete stored document; written to `out` when given, else returned."""
    state = repository.export_document(slug)
    if out is not None:
        Path(out).write_bytes((json.dumps(state, indent=2, ensure_ascii=ESCAPE_NON_ASCII) + "\n").encode())
    return state


def load(path: Path, slug: str | None = None, replace: bool = False) -> str:
    """Store an exported document; refuses an existing ledger unless replacing."""
    path = Path(path)
    slug = slug or path.stem
    if not legacy.core.SLUG_RE.match(slug):
        raise ValueError(f"invalid slug: {slug!r}")
    state = legacy.core.loads(path.read_bytes())
    if not isinstance(state, dict) or not isinstance(state.get("_meta"), dict) or "rev" not in state["_meta"]:
        raise ValueError(f"{path} is not an exported ledger document")
    repository.import_document(slug, state, replace=replace)
    return slug


def cutover() -> list:
    """Import every ledger file still in the ledger folder, after its verified backup; the slugs now stored."""
    legacy.adopt(repository)
    return sorted(summary["slug"] for summary in repository.list_summaries())
