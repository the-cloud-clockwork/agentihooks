#!/usr/bin/env python3
"""Ledger domain: the document shape, its validation and the ops that change it.

A ledger is a document plus `_meta` (rev, per-path stamps, an event log, members) stored by
the SQLite repository. Threads (comments, answers, notes) are lists of entries
{id, by, at, text[, edited_at, deleted]}; the operator and agents add, edit and delete
entries through ops. The seed parser reads ledger pages left in the ledger folder, once, when
they are imported.
"""

import difflib
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

import ledger_alerts
import ledger_answer
import ledger_close
import ledger_comments
import ledger_names
import ledger_notifications
import ledger_priorities
import ledger_relay
import ledger_size
import ledger_sources
import ledger_tasks
import ledger_time_left
import ledger_title
import ledger_verdict
import orjson

from scripts.swarm_ledger import ledger_groups, ledger_phases, ledger_rank

LEDGER_DIR = Path(os.environ.get("LEDGER_DIR", Path.home() / "development-ledger")).expanduser()
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,120}$")
SEED_RE = re.compile(r'(<script id="ledger-data" type="application/json">)(.*?)(</script>)', re.S)
PAGE_RE = re.compile(r'<meta name="ledger-page" content="([0-9a-f]+)">')
TEMPLATE = Path(__file__).resolve().parent / "template.html"
PALETTE = TEMPLATE.with_name("palette.css")
TOOLTIPS = TEMPLATE.with_name("tooltips.js")
MODULES = TEMPLATE.parent / "static" / "js"
SHELL = TEMPLATE.with_name("shell.html")
HOME = TEMPLATE.with_name("home.html")
TOKEN_RE = re.compile(r'<meta name="ledger-token" content="([A-Za-z0-9_-]{16,})">')
LEGACY_LINE_RE = re.compile(r"^([A-Za-z][\w.-]*)(?: [0-9:]+Z?| \([^)]*\))?: (.+)$")
LISTS = {
    "phases": ("title", "description", "done", "out_of_scope", "depends_on", "planning", "release"),
    "questions": ("text", "out_of_scope"),
    "followups": ("text", "done", "out_of_scope"),
    "tasks": (
        "title",
        "description",
        "phase",
        "lane",
        "state",
        "claimed_by",
        "issue_url",
        "pr_url",
        "done",
        "out_of_scope",
    ),
}
BOOL_FIELDS = ("done", "out_of_scope")
STATE_EVENTS = {"done": ("checked", "unchecked"), "out_of_scope": ("out of scope", "back in scope")}
THREADS = {
    "notes": ("comments",),
    "phases": ("comments",),
    "questions": ("answers", "comments"),
    "followups": ("comments",),
    "tasks": ("comments",),
}
AGENT_OPS = ("join", "leave", "ack", "claim", "set", "add_item", "retext", "gate_bypass", "gate_lift")
ARTIFACT_OPS = ("artifact_add", "artifact_delete", "artifact_restore", "artifact_purge")
AUTHOR_RE = re.compile(r"^[A-Za-z][\w.@-]{0,63}$")
NOUN = {"comments": "comment", "answers": "answer", "notes": "note", "chat": "message"}
CHAT_KEPT = 500
SYNC_COOLDOWN_MS = 5 * 60 * 1000
SYNC_KINDS = {"sync": "sync requested", "stats_sync": "stats sync requested"}
DEFAULT_CHAT_INSTRUCTIONS = (
    "Answer with agentihooks ledger say, in plain words for the operator: no times, ids, hashes, paths "
    "or capital labels. Under 100 words unless the operator asks, in a separate message, to expand."
)
EVENTS_KEPT = 2000
MAX_TEXT = 20000
LOG_MAX_BYTES = 5 << 20
LOCK = threading.Lock()

EXTENSION_OPS = {
    name: module
    for module in (
        ledger_priorities,
        ledger_notifications,
        ledger_tasks,
        ledger_rank,
        ledger_groups,
        ledger_title,
        ledger_names,
        ledger_close,
        ledger_size,
        ledger_sources,
        ledger_phases,
        ledger_relay,
        ledger_answer,
        ledger_verdict,
        ledger_alerts,
        ledger_time_left,
    )
    for name in module.OPS
}


def now_ms():
    return int(time.time() * 1000)


def paths(slug):
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        raise ValueError(f"invalid slug: {slug!r}")
    return LEDGER_DIR / f"{slug}.html", LEDGER_DIR / f"{slug}.json"


def _reject_constant(name):
    raise ValueError(f"{name} is not valid JSON")


def loads(text):
    return json.loads(text, parse_constant=_reject_constant)


PRETTY = orjson.OPT_INDENT_2 | orjson.OPT_PASSTHROUGH_DATETIME | orjson.OPT_PASSTHROUGH_DATACLASS


def pretty(value):
    try:
        return orjson.dumps(value, option=PRETTY).decode()
    except orjson.JSONEncodeError:
        return json.dumps(value, indent=2, ensure_ascii=False)


def legacy_entries(text, prefix, by_default, split):
    """A pre-thread string field as entries; comment blobs split per `<author>: ` line."""
    if not isinstance(text, str) or not text.strip():
        return []
    if not split:
        return [{"id": f"{prefix}-0", "by": by_default, "at": 0, "text": text.strip()}]
    entries = []
    for line in text.splitlines():
        match = LEGACY_LINE_RE.match(line.strip())
        if match or not entries:
            by, body = (match.group(1), match.group(2)) if match else (by_default, line.strip())
            if body:
                entries.append({"id": f"{prefix}-{len(entries)}", "by": by, "at": 0, "text": body})
        elif line.strip():
            entries[-1]["text"] += "\n" + line.strip()
    return entries


def time_left_value(value: object) -> Optional[int]:
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"[0-9]+", text):
            value = int(text)
        else:
            match = re.fullmatch(r"(?:([0-9]+)h)? ?(?:([0-9]+)m)?", text)
            if not match or not any(match.groups()):
                return None
            value = int(match[1] or 0) * 60 + int(match[2] or 0)
    return value if type(value) is int and value >= 0 else None


def normalize(doc):
    """Return a copy in the thread shape; string comments, answer and notes become entries."""
    if not isinstance(doc, dict):
        return doc
    doc = json.loads(json.dumps(doc))
    doc.pop("projection", None)
    doc["time_left_minutes"] = time_left_value(doc.get("time_left_minutes"))
    if not isinstance(doc.get("notes", []), list):
        doc["notes"] = legacy_entries(doc.get("notes"), "legacy-notes", "operator", False)
    doc.setdefault("notes", [])
    doc.setdefault("chat", [])
    doc.setdefault("priorities", [])
    doc.setdefault("notifications", [])
    doc.setdefault("artifacts", [])
    doc.setdefault("artifact_trash", [])
    doc.setdefault("tasks", [])
    for name in THREADS:
        for item in doc.get(name, []) if isinstance(doc.get(name), list) else []:
            if not isinstance(item, dict):
                continue
            key = f"legacy-{name}-{item.get('id')}"
            if name == "questions" and "answers" not in item:
                item["answers"] = legacy_entries(item.pop("answer", ""), f"{key}-answer", "operator", False)
            item.pop("answer", None)
            if not isinstance(item.get("comments", []), list):
                item["comments"] = legacy_entries(item["comments"], f"{key}-comments", "earlier", True)
            for thread in THREADS[name]:
                item.setdefault(thread, [])
    return doc


def validate_thread(where, entries):
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        raise ValueError(f"{where} must be a list of entries")
    ids = [e.get("id") for e in entries]
    if not all(isinstance(i, str) and i for i in ids) or len(set(ids)) != len(ids):
        raise ValueError(f"{where}: every entry needs a unique string id")
    for e in entries:
        if not isinstance(e.get("text", ""), str) or not isinstance(e.get("by", ""), str):
            raise ValueError(f"{where}/{e['id']}: text and by must be strings")


def validate(doc):
    if not isinstance(doc, dict):
        raise ValueError("seed is not a JSON object")
    if doc.get("time_left_minutes") is not None and (
        type(doc["time_left_minutes"]) is not int or time_left_value(doc["time_left_minutes"]) is None
    ):
        raise ValueError("time_left_minutes must be a nonnegative integer")
    for key in ("title", "overview", "orchestrator", "chat_instructions"):
        if not isinstance(doc.get(key, ""), str):
            raise ValueError(f"{key} must be a string")
    if not isinstance(doc.get("sources", []), list) or not all(isinstance(s, str) for s in doc.get("sources", [])):
        raise ValueError("sources must be a list of strings")
    validate_thread("notes", doc.get("notes", []))
    validate_thread("chat", doc.get("chat", []))
    for note in doc.get("notes", []):
        validate_thread(f"notes/{note['id']}/comments", note.get("comments", []))
    if "policy" in doc and not isinstance(doc["policy"], dict):
        raise ValueError("policy must be an object")
    for name, fields in LISTS.items():
        items = doc.get(name, [])
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            raise ValueError(f"{name} must be a list of objects")
        ids = [item.get("id") for item in items]
        if not all(isinstance(i, str) and i and "/" not in i for i in ids) or len(set(ids)) != len(ids):
            raise ValueError(f"every {name} item needs a unique string id without '/'")
        for item in items:
            for field in fields:
                expected = list if field == "depends_on" else bool if field in (*BOOL_FIELDS, "release") else str
                if field in item and not isinstance(item[field], expected):
                    raise ValueError(f"{name}/{item['id']}/{field} must be {expected.__name__}")
            for thread in THREADS[name]:
                validate_thread(f"{name}/{item['id']}/{thread}", item.get(thread, []))
            if name == "tasks":
                ledger_tasks.check_task(item)

    ledger_phases.validate(doc.get("phases", []))


def thread_paths(doc):
    yield "notes", doc["notes"]
    yield "chat", doc["chat"]
    for name in THREADS:
        for item in doc[name]:
            for thread in THREADS[name]:
                yield f"{name}/{item['id']}/{thread}", item.get(thread, [])


def thread_target(path):
    return path.rsplit("/", 1)[0] if "/" in path else ("chat" if path == "chat" else "")


def get_thread(doc, path):
    parts = path.split("/")
    if parts in (["notes"], ["chat"]):
        return doc[parts[0]]
    if len(parts) != 3 or parts[0] not in THREADS or parts[2] not in THREADS[parts[0]]:
        return None
    item = next((i for i in doc[parts[0]] if i["id"] == parts[1]), None)
    return None if item is None or item.get("deleted") else item.setdefault(parts[2], [])


def parse_seed(html):
    match = SEED_RE.search(html)
    if not match:
        raise ValueError('no <script id="ledger-data" type="application/json"> block')
    seed = normalize(loads(match.group(2)))
    validate(seed)
    return seed


def watch_path(slug, name):
    from scripts.swarm.naming import addresses

    paths = [LEDGER_DIR / ".sessions" / f"{slug}.{candidate}.watch" for candidate in addresses(name)]
    return next((path for path in paths if path.exists()), paths[0])


def static_assets():
    """Every file a page loads, by its path under /static/<page version>/."""
    static = MODULES.parent
    return {
        "palette.css": PALETTE,
        "tooltips.js": TOOLTIPS,
        **{f"css/{p.name}": p for p in sorted((static / "css").glob("*.css"))},
        **{f"js/{p.name}": p for p in sorted(MODULES.glob("*.js"))},
        **{f"home/{p.name}": p for p in sorted((static / "home").glob("*.js"))},
    }


def page_version(assets=None):
    if assets is None:
        assets = {name: path.read_bytes() for name, path in static_assets().items()}
    digest = hashlib.sha256(TEMPLATE.read_bytes() + SHELL.read_bytes() + HOME.read_bytes())
    for name, data in assets.items():
        digest.update(name.encode() + b"\0" + data)
    return digest.hexdigest()[:12]


def read_token(html):
    match = TOKEN_RE.search(html)
    return match.group(1) if match else None


def seed_text(doc, rev=None):
    body = doc if rev is None else {"_rev": rev, **doc}
    return "\n" + pretty(body).replace("<", "\\u003c") + "\n"


def rotate_if_full(path, limit=LOG_MAX_BYTES):
    try:
        if path.stat().st_size >= limit:
            path.replace(path.with_name(path.name + ".1"))
    except FileNotFoundError:
        pass


def append_capped(path, text, limit=LOG_MAX_BYTES):
    rotate_if_full(path, limit)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text)


def atomic_write(path, text):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    written = os.stat(tmp)
    os.replace(tmp, path)
    return written


def text_diff(old, new):
    lines = difflib.ndiff(old.splitlines(), new.splitlines())
    return "\n".join(line for line in lines if line[:1] in "-+")


def warnings(doc):
    found = []
    words = len(ledger_close.intro(doc["overview"]).split())
    if words > 200:
        found.append(f"overview has {words} words, limit 200")
    for phase in doc["phases"]:
        if len(phase["description"].split()) > 100:
            found.append(f"phase {phase['id']} description has {len(phase['description'].split())} words, limit 100")
    return found


def size_warning(text):
    return re.fullmatch(r"(overview|phase \S+ description) has \d+ words, limit \d+", text) is not None


class Context:
    """One sync's clock, rev, stamps and event log."""

    def __init__(self, meta, at):
        self.at, self.rev = at, meta["rev"] + 1
        self.stamps, self.events = meta["stamps"], []
        self.meta, self.dirty, self.refused, self.dropped = meta, False, [], []
        self.names = {}

    def author(self, name: str) -> str:
        if name not in self.names:
            from scripts.swarm.naming import resolve_name

            resolved = resolve_name(name)
            self.names[name] = resolved
            self.names[resolved] = resolved
        return self.names[name]

    def record(self, by, kind, target, **extra):
        self.events.append({"rev": self.rev, "at": self.at, "by": by, "kind": kind, "target": target, **extra})

    def stamp(self, path, by):
        self.stamps[path] = {"at": self.at, "rev": self.rev, "by": by}


def earliest(meta, at):
    """The ledger's start: the oldest real timestamp among its stamps and events (legacy entries carry at=0)."""
    stamps = [s["at"] for s in meta["stamps"].values() if s.get("at")]
    events = [e["at"] for e in meta["events"] if e.get("at")]
    return min([meta.get("created_at") or at, *stamps, *events])


def kind_of(path):
    return "chat" if path == "chat" else "comment"


def state_event(field, value):
    return STATE_EVENTS[field][0 if value else 1]


def set_state(item, field, value):
    """Done and out of scope exclude each other: setting one clears the other."""
    item[field] = value
    other = "out_of_scope" if field == "done" else "done"
    if value and other in item:
        item[other] = False


def apply_changes(doc, changes, ctx):
    """Checkbox changes: `base` is the value the page started from; a stale base loses."""
    rejected = []
    for change in changes:
        parts = change["path"].split("/")
        items = {i["id"]: i for i in doc.get(parts[0], [])} if parts[0] in LISTS else {}
        item = items.get(parts[1]) if len(parts) == 3 else None
        if item is None or parts[2] not in BOOL_FIELDS or parts[2] not in LISTS[parts[0]]:
            rejected.append(change["path"])
            continue
        value, current = bool(change.get("value")), item.get(parts[2], False)
        if "base" in change and change["base"] != current:
            rejected.append(change["path"])
            continue
        if value != current:
            set_state(item, parts[2], value)
            ctx.stamp(change["path"], "operator")
            ctx.record("operator", state_event(parts[2], value), "/".join(parts[:2]))
            if parts[2] == "out_of_scope":
                note = "Out of scope." if value else "Back in scope."
                item["comments"].append(
                    {"id": f"scope-{ctx.rev}-{parts[1]}", "by": "operator", "at": ctx.at, "text": note}
                )
    return rejected


def clear_chat(thread, op, target, ctx):
    if target != "chat":
        return False
    if thread:
        thread.clear()
        ctx.record("operator", "chat cleared", target, id=op["id"])
        ctx.stamp("chat", "operator")
    return True


def new_entry(op, by, at, text):
    entry = {"id": op["id"], "by": by, "at": at, "text": text}
    if op["thread"] == "notes":
        entry["comments"] = []
    return entry


def record_sync(doc, op, ctx):
    """An operator sync order; each kind rests SYNC_COOLDOWN_MS after it was last sent."""
    import ledger_gate
    import ledger_stats

    kind = SYNC_KINDS[op["op"]]
    last = max((e.get("at", 0) for e in ctx.meta["events"] if e.get("kind") == kind), default=0)
    failed = op["op"] == "stats_sync" and ctx.meta.get("stats_refresh", {}).get("state") == "failed"
    if ctx.at - last < SYNC_COOLDOWN_MS and not failed:
        return False
    if op["op"] == "sync":
        text = ledger_gate.sync_summary(doc, ctx.meta)
    else:
        text = ledger_stats.refresh(doc, ctx, op["id"])
    ctx.record("operator", kind, "", id=op["id"], text=text)
    return True


def apply_op(doc, op, ctx):
    if "by" in op:
        op = {**op, "by": ctx.author(op["by"])}
    if op["op"] in AGENT_OPS:
        import ledger_agent_ops

        return ledger_agent_ops.apply(doc, op, ctx)
    if op["op"] in SYNC_KINDS:
        return record_sync(doc, op, ctx)
    if op["op"] in EXTENSION_OPS:
        return EXTENSION_OPS[op["op"]].apply(doc, op, ctx)
    if op["op"] in ARTIFACT_OPS:
        import ledger_artifacts

        return ledger_artifacts.apply(doc, op, ctx)
    thread = get_thread(doc, op["thread"])
    if thread is None:
        return False
    target = thread_target(op["thread"])
    if op["op"] == "clear":
        return clear_chat(thread, op, target, ctx)
    noun, text = NOUN[op["thread"].rsplit("/", 1)[-1]], op.get("text", "")
    if "by" in op:
        done = ledger_comments.agent_thread_op(thread, op, ctx, target, noun)
        if done:
            ctx.stamp(op["thread"], op["by"])
        return done
    entry = next((e for e in thread if e["id"] == op["id"]), None)
    if op["op"] == "add":
        if entry is not None or not (text.strip() or op.get("attachments")):
            return entry is not None
        by = op.get("by", "operator")
        thread.append(new_entry(op, by, ctx.at, text))
        if op.get("attachments"):
            thread[-1]["attachments"] = op["attachments"]
        if by != "operator" and by in ctx.meta["members"]:
            ctx.meta["members"][by]["last_seen"] = ctx.at
        ctx.record(by, f"{noun} added", target, id=op["id"], text=text)
    elif entry is None or entry.get("deleted"):
        return False
    elif op["op"] == "edit":
        if text == entry["text"] or not text.strip():
            return text == entry["text"]
        ctx.record("operator", f"{noun} edited", target, id=op["id"], diff=text_diff(entry["text"], text))
        entry.update(text=text, edited_at=ctx.at)
    else:
        ctx.record("operator", f"{noun} deleted", target, id=op["id"], text=entry["text"])
        entry.update(text="", deleted=True, edited_at=ctx.at)
    if target.startswith("notes/"):
        note = next(n for n in doc["notes"] if n["id"] == target.split("/")[1])
        ctx.events[-1]["note_text"] = note["text"]
    ctx.stamp(op["thread"], "operator")
    return True


def _screened(op):
    if not op["thread"].endswith("/comments"):
        return op["text"]
    from hooks.context.conditions import LEDGER_WRITE
    from hooks.filters import check as filters

    return filters.screen(LEDGER_WRITE, op["text"])


def _check_chat_address(op, chat_add):
    if ("to" in op or "reply_to" in op) and not chat_add:
        raise ValueError("to and reply_to address only an agent chat message")
    if op.get("to", "operator") != "operator" or not isinstance(op.get("reply_to", ""), str):
        raise ValueError("an agent chat message goes to the operator or answers his line by its id")


def check_op(op, task_ids=()):
    if not isinstance(op, dict) or op.get("op") not in (
        "add",
        "edit",
        "delete",
        "clear",
        *SYNC_KINDS,
        *EXTENSION_OPS,
        *AGENT_OPS,
        *ARTIFACT_OPS,
    ):
        raise ValueError("each op needs op add, edit, delete, clear, sync or an agent op")
    if not isinstance(op.get("id"), str) or not op["id"]:
        raise ValueError("each op needs a string id")
    if op["op"] in SYNC_KINDS:
        if set(op) != {"op", "id"}:
            raise ValueError("sync is the operator's and takes only an id")
        return None
    if op["op"] in EXTENSION_OPS:
        return EXTENSION_OPS[op["op"]].check(op)
    if op["op"] in ARTIFACT_OPS:
        import ledger_artifacts

        return ledger_artifacts.check(op)
    if op["op"] in AGENT_OPS:
        import ledger_agent_ops

        return ledger_agent_ops.check(op, task_ids)
    if not isinstance(op.get("thread"), str) or not op["thread"]:
        raise ValueError("each op needs a string thread")
    if op["op"] in ("add", "edit") and (not isinstance(op.get("text"), str) or len(op["text"]) > MAX_TEXT):
        raise ValueError(f"add and edit need text up to {MAX_TEXT} characters")
    chat_add = op["op"] == "add" and op["thread"] == "chat" and "by" in op
    if "long" in op and not chat_add:
        raise ValueError("long is allowed only on an agent chat message")
    _check_chat_address(op, chat_add)
    if "attachments" in op:
        import ledger_media

        talks = op["thread"] == "chat" or op["thread"].endswith("/comments")
        if op["op"] != "add" or not talks:
            raise ValueError("attachments ride only on an add to chat or a comment thread")
        ledger_media.check(op["attachments"])
    if "by" in op:
        talks = op["op"] != "clear" and (op["thread"] == "chat" or op["thread"].endswith("/comments"))
        if not talks or not AUTHOR_RE.match(str(op["by"])) or op["by"] == "operator":
            raise ValueError("by is allowed only on agent chat and comment entries, as an agent name")
        if op["op"] in ("add", "edit"):
            op["text"] = _screened(op)
            ledger_comments.check(op["text"], kind_of(op["thread"]), op.get("long") is True, task_ids)


def check_body(body, task_ids=()):
    """Raise ValueError unless body is {"changes": [...], "ops": [...]} with well-formed members."""
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    changes, ops = body.get("changes", []), body.get("ops", [])
    if not isinstance(changes, list) or not isinstance(ops, list):
        raise ValueError("changes and ops must be lists")
    for change in changes:
        if not isinstance(change, dict) or not isinstance(change.get("path"), str):
            raise ValueError("each change needs a string path")
    for op in ops:
        check_op(op, task_ids)
    return changes, ops


def load_state(json_path, seed):
    from scripts.swarm_ledger.repository.legacy import load_state as read_state

    return read_state(json_path, seed, sys.modules[__name__])


def gated(gate, doc, op, ctx):
    return gate.apply(doc, op, ctx, apply_op) if gate else apply_op(doc, op, ctx)


def sync(slug, changes=None, ops=None, gate=None):
    from scripts.swarm_ledger.repository import repository

    return repository.bound(sys.modules[__name__]).apply_ops(slug, changes=changes, ops=ops, gate=gate)
