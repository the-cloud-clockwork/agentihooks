import hashlib
import json

from .errors import APIError


def revision(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def reply_size(reply: object) -> int:
    return len(json.dumps(reply, ensure_ascii=False).encode())


def bounded(reply: dict) -> dict:
    if reply_size(reply) > MAX_REPLY:
        raise APIError(413, "resource_too_large", "Use the explicit export operation for this resource")
    return reply


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


def resource_revision(doc: dict, path: str) -> str:
    raw = value(doc, path)
    if path == "metadata":
        raw = {**raw, "_meta": {key: item for key, item in raw["_meta"].items() if key not in ("rev", "updated_at")}}
    return revision(raw)


def thread_rows(doc: dict) -> list:
    return [
        {"path": f"{name}/{item['id']}/{thread}", "entry": entry}
        for name, fields in THREADS.items()
        for item in doc.get(name, [])
        for thread in fields
        for entry in item.get(thread, [])
    ]


def read(doc: dict, path: str, query: dict | None = None) -> dict:
    raw = value(doc, path)
    rev = resource_revision(doc, path)
    if isinstance(raw, list):
        return page(raw, rev, query or {})
    return bounded({"data": project(raw), "revision": rev})


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
    "alerts",
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
    if path == "threads":
        return thread_rows(doc)
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
    selected = []
    for row in rows[offset : offset + query.get("limit", 50)]:
        item = project(row)
        if isinstance(item, dict):
            item = {**item, "revision": revision(row)}
        end = offset + len(selected) + 1
        candidate = {
            "data": [*selected, item],
            "revision": rev,
            "next_cursor": f"{rev}:{end}" if end < len(rows) else None,
        }
        if reply_size(candidate) > MAX_REPLY:
            if not selected:
                raise APIError(413, "resource_too_large", "Use the explicit export operation for this resource")
            break
        selected.append(item)
    end = offset + len(selected)
    return bounded({"data": selected, "revision": rev, "next_cursor": f"{rev}:{end}" if end < len(rows) else None})


def swarm_read(status: dict | None, path: str, query: dict) -> dict:
    if status is None:
        raise APIError(404, "swarm_missing", "No swarm for this ledger")
    collections = {key: item for key, item in status.items() if isinstance(item, list)}
    if path == "swarm":
        data = {key: item for key, item in status.items() if key not in collections}
        return bounded({"data": data, "revision": revision(data), "collections": list(collections)})
    name = path.removeprefix("swarm/")
    if name not in collections:
        raise APIError(404, "resource_missing", "No such swarm resource")
    rows = collections[name]
    return page(rows, revision(rows), query)


def hierarchy_read(repository, slug: str, path: str) -> dict:
    from ..repository.hierarchy import KINDS, READS, text

    name, _, node = path.removeprefix("hierarchy").removeprefix("/").partition("/")
    if (name or "subtree") not in READS:
        raise APIError(404, "resource_missing", "No such hierarchy read")
    try:
        rows = repository.nodes(slug, name or "subtree", node or None)
    except KeyError:
        raise APIError(404, "resource_missing", "No such node") from None
    doc = {"phases": [], "tasks": [], **repository.read(slug, *KINDS)}
    items = {
        f"{key}/{item['id']}": item
        for key in KINDS
        for item in doc.get(key) or []
        if isinstance(item, dict) and text(item.get("id"))
    }
    data = [{**row, "state": node_state(row["kind"], items.get(row["node"], {}), doc)} for row in rows]
    return bounded({"data": data, "revision": revision(data)})


def node_state(kind: str, item: dict, doc: dict) -> str:
    from scripts.swarm.phase_state import lifecycle

    if kind == "phase":
        return lifecycle(item, doc)
    if item.get("out_of_scope"):
        return "out_of_scope"
    return item.get("state") or ("done" if item.get("done") else "open")
