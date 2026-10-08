import json
from dataclasses import dataclass

from .chunker import chunk_body
from .models import RecallRecord


@dataclass(frozen=True)
class _Origin:
    slug: str
    swarm_slug: str
    events: dict[str, dict]


def _first_events(document: dict) -> dict[str, dict]:
    events = {}
    for event in document.get("_meta", {}).get("events", []):
        events.setdefault(event["target"], event)
    return events


def _body(item: dict) -> str:
    parts = [item[field] for field in ("title", "description", "text", "overview") if item.get(field)]
    for field in ("contract", "proof"):
        if item.get(field):
            parts.append(f"## {field.title()}\n\n{json.dumps(item[field], ensure_ascii=False, sort_keys=True)}")
    parts.extend(item[field] for field in ("pr_url", "issue_url") if item.get(field))
    return "\n\n".join(parts)


def _entry(origin: _Origin, item: dict, ref: str, kind: str, parent_ref: str) -> list[RecallRecord]:
    if item.get("deleted"):
        return []
    source = origin.events.get(ref, {}) if kind in ("task", "question", "followup") else item
    title = item.get("title") or item.get("text", "")
    result = [
        RecallRecord(
            key=f"{origin.slug}/{ref}#{index}",
            ledger_slug=origin.slug,
            swarm_slug=origin.swarm_slug,
            kind=kind,
            ref=ref,
            parent_ref=parent_ref,
            author=source.get("by", ""),
            time=source.get("at", 0),
            title=title,
            text=chunk,
            chunk_index=index,
        )
        for index, chunk in enumerate(chunk_body(_body(item)) or [""])
    ]
    for thread, child_kind in (("comments", "comment"), ("answers", "answer")):
        for child in item.get(thread, []):
            result.extend(_entry(origin, child, f"{ref}/{thread}/{child['id']}", child_kind, ref))
    return result


def _parent(item: dict, kind: str) -> str:
    if kind == "task" and item.get("phase"):
        return f"phases/{item['phase']}"
    if kind == "artifact" and item.get("task"):
        return f"tasks/{item['task']}"
    return "ledger"


def extract_ledger(slug: str, document: dict, *, swarm_slug: str = "") -> list[RecallRecord]:
    origin = _Origin(slug, swarm_slug, _first_events(document))
    root = {
        "title": document.get("title", ""),
        "overview": document.get("overview", ""),
        "at": document.get("_meta", {}).get("created_at", 0),
    }
    result = _entry(origin, root, "ledger", "ledger", "") if _body(root) else []
    for collection, kind in (
        ("phases", "phase"),
        ("tasks", "task"),
        ("questions", "question"),
        ("followups", "followup"),
        ("notes", "note"),
        ("chat", "chat"),
        ("artifacts", "artifact"),
    ):
        for item in document.get(collection, []):
            ref = f"{collection}/{item['id']}"
            result.extend(_entry(origin, item, ref, kind, _parent(item, kind)))
    return result
