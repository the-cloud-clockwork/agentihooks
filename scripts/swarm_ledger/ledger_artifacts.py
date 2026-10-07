"""Files agents publish for operator review: stored by content hash beside the ledger media, listed in `artifacts`."""

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import PurePath

import ledger_comments
import ledger_core as core
import ledger_media as media

OPS = ("artifact_add", "artifact_delete", "artifact_restore", "artifact_purge")
TRASH = "artifact_trash"
KEEP_DAYS = 30
DAY_MS = 24 * 60 * 60 * 1000
REFUSED = (
    "an artifact is only a file the operator asked for: mark its task as artifact requested or name his message "
    "with request, and proofs go on the task proof and the pull request"
)
ADD_KEYS = {"op", "id", "by", "task", "title", "file"}
MAX_BYTES = 8 << 20
MAX_TITLE = 200
SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
TEXT_TYPES = {"md": "text/markdown", "json": "application/json", "svg": "image/svg+xml"}
SUFFIXES = {".md": "md", ".markdown": "md", ".json": "json", ".svg": "svg"}
TYPES = {**TEXT_TYPES, "png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}
ID_RE = re.compile(rf"^[0-9a-f]{{64}}\.({'|'.join(TYPES)})$")
AUTHOR_RE = re.compile(r"^[A-Za-z][\w.@-]{0,63}$")
TASK_RE = re.compile(r"^[^/\s]{0,64}$")
DROPPED = {"script", "foreignObject", "iframe", "object", "embed", "handler", "listener"}

ET.register_namespace("", SVG_NS)
ET.register_namespace("xlink", XLINK_NS)


def _local(name):
    return name.rsplit("}", 1)[-1]


def sanitize_svg(text):
    """The SVG without scripts, event handlers, embedded documents or links that leave the drawing."""
    if "<!ENTITY" in text or "<!DOCTYPE" in text:
        raise media.Refused(415, "an SVG may not declare a doctype or entities")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise media.Refused(415, f"the SVG does not parse: {exc}") from exc
    if root.tag != f"{{{SVG_NS}}}svg":
        raise media.Refused(415, "an SVG needs an svg root in the SVG namespace")
    for node in list(root.iter()):
        for child in list(node):
            if _local(child.tag) in DROPPED:
                node.remove(child)
        for attr, value in list(node.attrib.items()):
            name, value = _local(attr).lower(), value.strip().lower()
            if name.startswith("on") or (name == "href" and not value.startswith(("#", "data:image/"))):
                del node.attrib[attr]
    return ET.tostring(root, encoding="unicode").encode("utf-8")


def _text(name, data):
    ext = SUFFIXES.get(PurePath(name or "").suffix.lower())
    if ext is None:
        raise media.Refused(415, "only markdown, JSON, SVG, PNG, JPEG, WebP or GIF files are accepted")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise media.Refused(415, "the file is not UTF-8 text") from exc
    if ext == "json":
        try:
            json.loads(text)
        except ValueError as exc:
            raise media.Refused(415, f"the file is not valid JSON: {exc}") from exc
    if ext == "svg":
        data = sanitize_svg(text)
    return ext, data, {"type": TEXT_TYPES[ext]}


def store(slug, name, data):
    if len(data) > MAX_BYTES:
        raise media.Refused(413, f"an artifact may be at most {MAX_BYTES >> 20} MB")
    try:
        kind, width, height = media.inspect(data)
        ext, fields = media.EXTENSIONS[kind], {"type": kind, "width": width, "height": height}
    except media.Refused:
        ext, data, fields = _text(name, data)
    artifact_id = f"{hashlib.sha256(data).hexdigest()}.{ext}"
    path = media.folder(slug) / artifact_id
    media.write_file(path, data)
    return {"id": artifact_id, **fields, "size": len(data)}


def path_of(slug, artifact_id):
    if not isinstance(artifact_id, str) or not ID_RE.match(artifact_id):
        raise ValueError("not an artifact id")
    path = media.folder(slug) / artifact_id
    if not path.is_file():
        raise ValueError(f"no stored artifact {artifact_id}")
    return path


def entry(slug, artifact_id):
    data = path_of(slug, artifact_id).read_bytes()
    ext = artifact_id.rsplit(".", 1)[1]
    if ext in TEXT_TYPES:
        return {"id": artifact_id, "type": TEXT_TYPES[ext], "size": len(data)}
    kind, width, height = media.inspect(data)
    return {"id": artifact_id, "type": kind, "width": width, "height": height, "size": len(data)}


def resolve(slug, ops):
    """Each published file rebuilt from the store, so the JSON holds only what the server measured."""
    for op in ops:
        if op.get("op") == "artifact_add":
            op["file"] = entry(slug, op["file"]["id"])
    return ops


def check(op):
    if op["op"] == "artifact_add":
        return check_add(op)
    if op["op"] == "artifact_purge":
        if set(op) != {"op", "id", "by"} or not AUTHOR_RE.match(str(op["by"])) or op["by"] == "operator":
            raise ValueError("artifact_purge takes id and by, an agent name other than operator")
        return None
    if set(op) != {"op", "id", "target"} or not isinstance(op["target"], str) or not op["target"]:
        raise ValueError(f"{op['op']} is the operator's and takes only id and target, an artifact id")
    return None


def check_add(op):
    if not ADD_KEYS <= set(op) <= ADD_KEYS | {"request", "plan"}:
        raise ValueError("artifact_add takes id, by, task, title, file and an optional request or plan")
    if "plan" in op and op["plan"] is not True:
        raise ValueError("plan must be true, marking a published plan")
    if not isinstance(op["by"], str) or not AUTHOR_RE.match(op["by"]) or op["by"] == "operator":
        raise ValueError("artifact_add needs `by`, an agent name other than operator")
    if not isinstance(op["task"], str) or not TASK_RE.match(op["task"]):
        raise ValueError("task must be a task id or empty")
    if "request" in op and (not isinstance(op["request"], str) or not op["request"]):
        raise ValueError("request must be the id of the operator message that asked for the file")
    if not isinstance(op["title"], str) or not op["title"].strip() or len(op["title"]) > MAX_TITLE:
        raise ValueError(f"title must be text of at most {MAX_TITLE} characters")
    if not isinstance(op["file"], dict) or not ID_RE.match(str(op["file"].get("id"))):
        raise ValueError("file needs a stored artifact id")
    ledger_comments.check(op["title"], "item")


def operator_asked(doc, entry_id):
    if not entry_id:
        return False
    return any(
        entry.get("id") == entry_id and entry.get("by") == "operator" and not entry.get("deleted")
        for _, thread in core.thread_paths(doc)
        for entry in thread
    )


def refusal(doc: dict, op: dict, members: dict) -> str:
    from scripts.swarm.naming import lane_of

    member = members.get(op["by"], {})
    if op["by"] not in members:
        return "join the ledger first and name a task it holds"
    if op["task"] == "master" and member.get("role") == "orchestrator" and lane_of(op["by"]) == "master":
        return "" if operator_asked(doc, op.get("request")) else REFUSED
    task = next((t for t in doc["tasks"] if t["id"] == op["task"]), None)
    if op["task"] and task is None:
        return "join the ledger first and name a task it holds"
    requested = op.get("plan") is True or (task and task.get("artifact") is True)
    return "" if requested or operator_asked(doc, op.get("request")) else REFUSED


def _add(doc, op, ctx):
    rows = doc.setdefault("artifacts", [])
    if any(row["id"] == op["id"] for row in rows):
        return True
    reason = refusal(doc, op, ctx.meta["members"])
    if reason:
        if reason == REFUSED:
            ctx.refused.append(reason)
        return False
    from scripts.swarm.naming import lane_of

    if (
        op["task"] == "master"
        and ctx.meta["members"][op["by"]].get("role") == "orchestrator"
        and lane_of(op["by"]) == "master"
    ):
        op = {**op, "task": ""}
    title = op["title"].strip()
    row = {"id": op["id"], "title": title, "by": op["by"], "task": op["task"], "at": ctx.at, "file": op["file"]}
    for key in ("request", "plan"):
        if key in op:
            row[key] = op[key]
    rows.append(row)
    ctx.stamp("artifacts", op["by"])
    ctx.record(
        op["by"], "artifact added", f"tasks/{op['task']}" if op["task"] else "artifacts", id=op["id"], text=title
    )
    return True


def _move(doc, op, ctx, source, dest):
    row = next((r for r in doc[source] if r["id"] == op["target"]), None)
    if row is None:
        return any(r["id"] == op["target"] for r in doc[dest])
    doc[source].remove(row)
    if dest == TRASH:
        row["deleted_at"] = ctx.at
    else:
        del row["deleted_at"]
    doc[dest].append(row)
    kind = "artifact deleted" if dest == TRASH else "artifact restored"
    ctx.record("operator", kind, "artifacts", id=row["id"], text=row["title"])
    return True


def _purge(doc, op, ctx):
    if op["by"] not in ctx.meta["members"]:
        return False
    rows = doc["artifacts"] + doc[TRASH]
    doc["artifacts"], doc[TRASH] = [], []
    ctx.dropped.extend(row["file"]["id"] for row in rows)
    ctx.record(op["by"], "artifacts purged", "artifacts", id=op["id"], count=len(rows))
    return True


def apply(doc, op, ctx):
    if op["op"] == "artifact_add":
        return _add(doc, op, ctx)
    if op["op"] == "artifact_purge":
        return _purge(doc, op, ctx)
    if op["op"] == "artifact_delete":
        return _move(doc, op, ctx, "artifacts", TRASH)
    return _move(doc, op, ctx, TRASH, "artifacts")


def in_use(doc):
    used = {row["file"]["id"] for key in ("artifacts", TRASH) for row in doc[key]}
    for _, thread in core.thread_paths(doc):
        for entry in thread:
            used.update(att["id"] for att in entry.get("attachments", []))
    return used


def sweep(slug, doc, ctx):
    trash = doc[TRASH]
    expired = [row for row in trash if ctx.at - row["deleted_at"] > KEEP_DAYS * DAY_MS]
    if expired:
        doc[TRASH] = [row for row in trash if row not in expired]
        ctx.dirty = True
    dropped = {row["file"]["id"] for row in expired} | set(ctx.dropped)
    used = in_use(doc)
    media.sweep(slug, used, ctx.at)
    for file_id in dropped - used:
        (media.folder(slug) / file_id).unlink(missing_ok=True)
