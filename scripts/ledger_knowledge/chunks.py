"""Structure-aware parent-child chunks of one ledger document: ledger, phase, task, proof and comment."""

import json
from dataclasses import dataclass
from typing import Any

from scripts.recall.chunker import chunk_body

Document = dict[str, Any]
WINDOW = 600
ROOT = "ledger"
COLLECTIONS = (
    ("phases", "phase"),
    ("tasks", "task"),
    ("questions", "question"),
    ("followups", "followup"),
    ("notes", "note"),
)


@dataclass(frozen=True)
class Chunk:
    """One embedded piece of a ledger item; parent is the ref of the item holding it."""

    key: str
    ref: str
    parent: str
    kind: str
    title: str
    text: str


def _text(item: Document) -> str:
    return "\n\n".join(str(item[field]) for field in ("title", "description", "text", "overview") if item.get(field))


def _proof(proof: object) -> str:
    return proof if isinstance(proof, str) else json.dumps(proof, ensure_ascii=False, sort_keys=True)


def _pieces(ref: str, parent: str, kind: str, title: str, text: str) -> list[Chunk]:
    return [
        Chunk(f"{ref}#{index}", ref, parent, kind, title, piece)
        for index, piece in enumerate(chunk_body(text, window=WINDOW) or [title])
    ]


def _item(item: Document, ref: str, parent: str, kind: str) -> list[Chunk]:
    if item.get("deleted"):
        return []
    title = str(item.get("title") or item.get("text") or "")
    result = _pieces(ref, parent, kind, title, _text(item))
    if item.get("proof"):
        result.extend(_pieces(f"{ref}/proof", ref, "proof", title, _proof(item["proof"])))
    for comment in item.get("comments", []):
        result.extend(_item(comment, f"{ref}/comments/{comment['id']}", ref, "comment"))
    return result


def _parent(item: Document, kind: str) -> str:
    if kind == "task" and item.get("phase"):
        return f"phases/{item['phase']}"
    return ROOT


def chunk_ledger(document: Document) -> list[Chunk]:
    """Every chunk of the ledger, each task under its phase and each proof and comment under its item."""
    root: Document = {"title": document.get("title", ""), "overview": document.get("overview", "")}
    result = _pieces(ROOT, "", ROOT, root["title"], _text(root))
    for collection, kind in COLLECTIONS:
        for item in document.get(collection, []):
            result.extend(_item(item, f"{collection}/{item['id']}", _parent(item, kind), kind))
    return result
