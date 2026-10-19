import hashlib
import json

from scripts.swarm_ledger import models, ports

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
    if path == "tasks":
        meta = doc.get("_meta", {})
        return f"{meta.get('task_revision', meta.get('rev', 0)):064x}"
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


def collection_revisions(rows: list) -> tuple[str, list]:
    digest = hashlib.sha256(b"[")
    items = []
    for index, row in enumerate(rows):
        encoded = json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        if index:
            digest.update(b",")
        digest.update(encoded)
        items.append(hashlib.sha256(encoded).hexdigest() if isinstance(row, dict) else None)
    digest.update(b"]")
    return digest.hexdigest(), items


def read(doc: dict, path: str, query: dict | None = None, cache: dict | None = None) -> dict:
    raw = value(doc, path)
    if isinstance(raw, list):
        hash_page = path == "tasks"
        if cache is None:
            return page(raw, resource_revision(doc, path), query or {}, hash_page=hash_page)
        key = (collection_revisions, path)
        if key not in cache:
            cache[key] = collection_revisions(raw)
        digest, items = cache[key]
        rev = resource_revision(doc, path) if hash_page else digest
        return page(raw, rev, query or {}, items, hash_page)
    return bounded({"data": project(raw), "revision": resource_revision(doc, path)})


COLLECTIONS = models.COLLECTIONS
THREADS = models.THREADS
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


def page(rows: list, rev: str, query: dict, revisions: list | None = None, hash_page: bool = False) -> dict:
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
    size = reply_size(envelope([], rev, None, hash_page))
    for row in rows[offset : offset + query.get("limit", 50)]:
        item = project(row)
        end = offset + len(selected) + 1
        if isinstance(item, dict):
            item = {**item, "revision": revisions[end - 1] if revisions is not None else revision(row)}
        next_cursor = f"{rev}:{end}" if end < len(rows) else None
        item_size = reply_size(item) + (2 if selected else 0)
        cursor_size = reply_size(next_cursor) - 4
        if size + item_size + cursor_size > MAX_REPLY:
            if not selected:
                raise APIError(413, "resource_too_large", "Use the explicit export operation for this resource")
            break
        selected.append(item)
        size += item_size
    end = offset + len(selected)
    next_cursor = f"{rev}:{end}" if end < len(rows) else None
    reply = envelope(selected, rev, next_cursor, hash_page)
    return reply if selected else bounded(reply)


def envelope(rows: list, rev: str, next_cursor: str | None, hash_page: bool) -> dict:
    reply = {"data": rows, "revision": rev, "next_cursor": next_cursor}
    if hash_page:
        reply["page_hash"] = revision(rows)
    return reply


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


def hierarchy_read(repository, slug: str, path: str, query: dict) -> dict:
    from ..repository.hierarchy import KINDS, READS, text

    name, _, node = path.removeprefix("hierarchy").removeprefix("/").partition("/")
    if (name or "subtree") not in READS:
        raise APIError(404, "resource_missing", "No such hierarchy read")
    try:
        rows = repository.nodes(slug, name or "subtree", node or None)
    except KeyError:
        raise APIError(404, "resource_missing", "No such node") from None
    doc = {"tasks": [], **repository.current(slug).state}
    items = {
        f"{key}/{item['id']}": item
        for key in KINDS
        for item in doc.get(key) or []
        if isinstance(item, dict) and text(item.get("id"))
    }
    data = [{**row, "state": node_state(row["kind"], items.get(row["node"], {}), doc)} for row in rows]
    return page(data, revision(data), query)


def node_state(kind: str, item: dict, doc: dict) -> str:
    if kind == "phase":
        return ports.swarm().phase_state.lifecycle(item, doc)
    if item.get("out_of_scope"):
        return "out_of_scope"
    return item.get("state") or ("done" if item.get("done") else "open")
