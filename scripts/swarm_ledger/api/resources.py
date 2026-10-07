import hashlib
import json

from .errors import APIError


def revision(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def metadata(doc: dict) -> dict:
    fields = (
        "size",
        "title",
        "overview",
        "orchestrator",
        "chat_instructions",
        "policy",
        "time_left_minutes",
        "closed_at",
        "summary",
    )
    result = {key: doc[key] for key in fields if key in doc}
    result["_meta"] = {
        key: doc["_meta"][key]
        for key in ("rev", "updated_at", "created_at", "size", "seed_error")
        if key in doc["_meta"]
    }
    return result


def read(doc: dict, path: str, query: dict | None = None) -> dict:
    raw = value(doc, path)
    rev = revision(raw)
    if isinstance(raw, list):
        return page(raw, rev, query or {})
    data = project(raw)
    if len(json.dumps(data).encode()) > MAX_REPLY:
        raise APIError(413, "resource_too_large", "Use the explicit export operation for this resource")
    return {"data": data, "revision": rev}


COLLECTIONS = (
    "tasks",
    "phases",
    "questions",
    "followups",
    "notes",
    "chat",
    "priorities",
    "notifications",
    "artifacts",
    "artifact_trash",
    "sources",
)
THREADS = {
    "tasks": ("comments",),
    "phases": ("comments",),
    "questions": ("comments", "answers"),
    "followups": ("comments",),
    "notes": ("comments",),
}
MAX_REPLY = 262144


def value(doc: dict, path: str) -> object:
    parts = path.split("/")
    name = parts[0]
    if path == "metadata":
        return metadata(doc)
    if path == "counts":
        return {name: len(doc.get(name, [])) for name in COLLECTIONS}
    if path == "events":
        return doc["_meta"].get("events", [])
    if name == "members":
        members = doc["_meta"].get("members", {})
        if len(parts) == 1:
            return [{"id": key, **entry} for key, entry in members.items()]
        if len(parts) == 2 and parts[1] in members:
            return {"id": parts[1], **members[parts[1]]}
    if name in COLLECTIONS:
        rows = doc.get(name, [])
        if len(parts) == 1:
            return rows
        item = next((row for row in rows if isinstance(row, dict) and row.get("id") == parts[1]), None)
        if item is not None and len(parts) == 2:
            return item
        if item is not None and len(parts) == 3 and parts[2] in THREADS.get(name, ()):
            return item.get(parts[2], [])
    raise APIError(404, "resource_missing", "No such resource")


def project(raw: object) -> object:
    if not isinstance(raw, dict):
        return raw
    result = {key: item for key, item in raw.items() if key not in ("comments", "answers")}
    for key in ("comments", "answers"):
        if key in raw:
            result[f"{key}_count"] = len(raw[key])
    return result


def page(rows: list, rev: str, query: dict) -> dict:
    from .schemas import PAGINATION, validate

    validate(PAGINATION, query)
    offset = 0
    cursor = query.get("cursor")
    if cursor:
        cursor_rev, offset_text = cursor.split(":")
        if cursor_rev != rev:
            raise APIError(409, "revision_conflict", "Collection changed; restart pagination")
        offset = int(offset_text)
    limit = query.get("limit", 50)
    selected, size = [], 0
    for row in rows[offset : offset + limit]:
        item = project(row)
        if isinstance(item, dict):
            item = {**item, "revision": revision(row)}
        entry_size = len(json.dumps(item).encode())
        if size + entry_size > MAX_REPLY:
            if not selected:
                raise APIError(413, "resource_too_large", "Use the explicit export operation for this resource")
            break
        selected.append(item)
        size += entry_size
    end = offset + len(selected)
    return {"data": selected, "revision": rev, "next_cursor": f"{rev}:{end}" if end < len(rows) else None}


def swarm_read(status: dict | None, path: str, query: dict) -> dict:
    if status is None:
        raise APIError(404, "swarm_missing", "No swarm for this ledger")
    collections = {key: item for key, item in status.items() if isinstance(item, list)}
    if path == "swarm":
        data = {key: item for key, item in status.items() if key not in collections}
        return {"data": data, "revision": revision(data), "collections": list(collections)}
    name = path.removeprefix("swarm/")
    if name not in collections:
        raise APIError(404, "resource_missing", "No such swarm resource")
    rows = collections[name]
    return page(rows, revision(rows), query)
