#!/usr/bin/env python3
"""Ledger state: the HTML seed block, the JSON store and the merge between them.

The HTML file carries the document in <script id="ledger-data">, stamped with the `_rev`
the server wrote; agents edit it. The JSON file carries the same document plus `_meta`
(rev, per-path stamps, an event log and the seeds written at recent revs). Fields an
agent changed relative to the seed at its `_rev` are agent edits. Threads (comments,
answers, notes) are lists of entries {id, by, at, text[, edited_at, deleted]}; the
operator adds, edits and deletes entries through ops, agents append entries in the seed.
"""

import difflib
import hashlib
import json
import os
import re
import shlex
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
SEED_ADD_COMMANDS = {
    "tasks": (
        "task",
        "task add <id>",
        ("phase", "lane", "description", "depends_on", "territory", "gain", "kind", "profile", "rank", "difficulty"),
    ),
    "followups": ("follow up", "followup add", ()),
    "phases": ("phase", "phase add <id>", ("description", "depends_on", "planning", "release")),
}
STATE_EVENTS = {"done": ("checked", "unchecked"), "out_of_scope": ("out of scope", "back in scope")}
THREADS = {
    "notes": ("comments",),
    "phases": ("comments",),
    "questions": ("answers", "comments"),
    "followups": ("comments",),
    "tasks": ("comments",),
}
SCALARS = ("title", "overview", "sources", "orchestrator", "chat_instructions", "policy", "time_left_minutes")
AGENT_OPS = ("join", "leave", "ack", "claim", "set", "add_item", "retext", "gate_bypass", "gate_lift")
ARTIFACT_OPS = ("artifact_add", "artifact_delete", "artifact_restore", "artifact_purge")
OPERATOR_THREADS = re.compile(r"^(notes|questions/[^/]+/answers)$")
AUTHOR_RE = re.compile(r"^[A-Za-z][\w.@-]{0,63}$")
NOUN = {"comments": "comment", "answers": "answer", "notes": "note", "chat": "message"}
CHAT_KEPT = 500
SYNC_COOLDOWN_MS = 5 * 60 * 1000
SYNC_KINDS = {"sync": "sync requested", "stats_sync": "stats sync requested"}
DEFAULT_CHAT_INSTRUCTIONS = (
    "Answer with agentihooks ledger say, in plain words for the operator: no times, ids, hashes, paths "
    "or capital labels. Under 100 words unless the operator asks, in a separate message, to expand."
)
SEEDS_KEPT = 5
EVENTS_KEPT = 2000
MAX_TEXT = 20000
LOG_MAX_BYTES = 5 << 20
MISSING = object()
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


def flatten(doc):
    """Every non-thread field as path -> value; list paths hold the id order."""
    flat = {key: doc.get(key, [] if key == "sources" else "") for key in SCALARS}
    for name, fields in LISTS.items():
        items = doc.get(name, [])
        flat[name] = [item["id"] for item in items]
        for item in items:
            for field in fields:
                if field in ("depends_on", "planning", "release") and field not in item:
                    continue
                flat[f"{name}/{item['id']}/{field}"] = item.get(field, False if field in BOOL_FIELDS else "")
    return flat


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
    return "\n" + json.dumps(body, indent=2, ensure_ascii=False).replace("<", "\\u003c") + "\n"


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
    os.replace(tmp, path)


def write_if_changed(path, text):
    try:
        if path.read_bytes() == text.encode():
            return
    except FileNotFoundError:
        pass
    atomic_write(path, text)


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

    def record(self, by, kind, target, **extra):
        self.events.append({"rev": self.rev, "at": self.at, "by": by, "kind": kind, "target": target, **extra})

    def stamp(self, path, by):
        self.stamps[path] = {"at": self.at, "rev": self.rev, "by": by}


def earliest(meta, at):
    """The ledger's start: the oldest real timestamp among its stamps and events (legacy entries carry at=0)."""
    stamps = [s["at"] for s in meta["stamps"].values() if s.get("at")]
    events = [e["at"] for e in meta["events"] if e.get("at")]
    return min([meta.get("created_at") or at, *stamps, *events])


def merge_order(current, base, new):
    """Items are never removed: an id missing from the seed stays, appended at the end."""
    kept = [i for i in new if i in current or i not in base]
    return kept + [i for i in current if i not in kept]


def agent_entry(entry, ctx):
    by = entry.get("by") or "agent"
    return {"id": entry["id"], "by": "agent" if by == "operator" else by, "at": ctx.at, "text": entry.get("text", "")}


def new_item(name, seed_item, ctx):
    item = {k: v for k, v in seed_item.items() if k not in THREADS[name] and not (name == "phases" and k == "review")}
    for thread in THREADS[name]:
        operator_only = thread == "answers"
        item[thread] = [] if operator_only else [agent_entry(e, ctx) for e in seed_item.get(thread, [])]
    return item


def refuse_seed_adds(doc, base_doc, seed, ctx):
    """New tasks, follow ups and phases come only from the ledger add commands."""
    kept = {}
    for name, (noun, command, fields) in SEED_ADD_COMMANDS.items():
        known = {i["id"] for i in doc[name]} | {i["id"] for i in base_doc[name]}
        kept[name] = [i for i in seed[name] if i["id"] in known]
        for item in seed[name]:
            if item["id"] not in known:
                label = item.get("text") or item.get("title", "")
                ctx.refused.append(
                    f'The page added the {noun} "{label}", which was not added. Add it with '
                    f"{seed_add_command(command, label, item, fields)}"
                )
    return {**seed, **kept}


def seed_add_command(command, label, item, fields):
    flags = []
    for field in fields:
        value, flag = item.get(field), f"--{field.replace('_', '-')}"
        if value is True:
            flags.append(flag)
        elif value:
            value = ",".join(value) if isinstance(value, list) else str(value)
            flags.append(f"{flag} {shlex.quote(value)}")
    return " ".join(["agentihooks ledger --slug <slug> --as <name>", command, shlex.quote(label), *flags])


def reconcile_fields(doc, base_doc, seed, ctx):
    seed = refuse_seed_adds(doc, base_doc, seed, ctx)
    if not ledger_phases.seed_graph_valid(doc["phases"], base_doc["phases"], seed["phases"], ctx):
        seed = {**seed, "phases": base_doc["phases"]}
    base, new, flat = flatten(base_doc), flatten(seed), flatten(doc)
    items = {name: {i["id"]: i for i in doc[name]} for name in LISTS}
    for name in LISTS:
        if new[name] != base[name]:
            seed_items = {i["id"]: i for i in seed[name]}
            for item_id in new[name]:
                if item_id not in items[name] and item_id not in base[name]:
                    items[name][item_id] = new_item(name, seed_items[item_id], ctx)
                    ctx.record(
                        "agent",
                        "added",
                        f"{name}/{item_id}",
                        text=seed_items[item_id].get("text") or seed_items[item_id].get("title", ""),
                    )
            doc[name] = [items[name][i] for i in merge_order(flat[name], base[name], new[name]) if i in items[name]]
    for path, value in new.items():
        if path in LISTS or base.get(path, MISSING) == value or flat.get(path, MISSING) == value:
            continue
        if not apply_seed_field(doc, items, path, value, ctx):
            continue
        parts = path.split("/")
        ctx.stamp(path, "agent")
        kind = state_event(parts[-1], value) if parts[-1] in BOOL_FIELDS else f"{parts[-1]} changed"
        ctx.record("agent", kind, "/".join(parts[:2]) if len(parts) == 3 else path)


def apply_seed_field(doc, items, path, value, ctx):
    parts = path.split("/")
    if len(parts) == 1:
        doc[path] = value
        return True
    item = items[parts[0]].get(parts[1])
    if item is None or parts[2] == "text" and ledger_comments.refused(value, "item", f"agent text of {path}", ctx):
        return False
    if parts[2] in BOOL_FIELDS:
        set_state(item, parts[2], value)
    else:
        item[parts[2]] = value
    return True


def reconcile_threads(doc, base_doc, seed, ctx):
    base = {p: {e["id"]: e for e in t} for p, t in thread_paths(base_doc)}
    for path, seed_thread in thread_paths(seed):
        current = get_thread(doc, path)
        if current is None or OPERATOR_THREADS.match(path):
            continue
        by_id = {e["id"]: e for e in current}
        old = base.get(path, {})
        target, noun = thread_target(path), NOUN[path.rsplit("/", 1)[-1]]
        for entry in seed_thread:
            have, was = by_id.get(entry["id"]), old.get(entry["id"])
            if have is None and was is None and entry.get("text", "").strip():
                if not seed_entry(current, path, agent_entry(entry, ctx), ctx):
                    continue
            elif (
                have
                and was
                and have["by"] != "operator"
                and not have.get("deleted")
                and entry.get("text", "") != was.get("text", "")
                and entry.get("text", "") != have["text"]
                and not ledger_comments.refused(
                    entry.get("text", ""), kind_of(path), f"{have['by']} edit on {path}", ctx
                )
            ):
                diff = text_diff(have["text"], entry.get("text", ""))
                have.update(text=entry.get("text", ""), edited_at=ctx.at)
                ctx.record(have["by"], f"{noun} edited", target, id=entry["id"], diff=diff)
            else:
                continue
            ctx.stamp(path, "agent")


def kind_of(path):
    return "chat" if path == "chat" else "comment"


def seed_entry(thread, path, entry, ctx):
    """A new agent entry written into the HTML seed: filtered, and a comment amends the agent's own."""
    if ledger_comments.refused(entry["text"], kind_of(path), f"{entry['by']} {kind_of(path)} on {path}", ctx):
        return False
    target, noun = thread_target(path), NOUN[path.rsplit("/", 1)[-1]]
    if noun == "comment":
        ledger_comments.post_status(thread, entry["by"], entry["id"], entry["text"], ctx, target)
    else:
        thread.append(entry)
        ctx.record(entry["by"], f"{noun} added", target, id=entry["id"], text=entry["text"])
    return True


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
        from scripts.swarm.naming import resolve_name

        op = {**op, "by": resolve_name(op["by"])}
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
        thread.append({"id": op["id"], "by": by, "at": ctx.at, "text": text})
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
    from scripts.swarm_ledger.repository.file import load_state as read_state

    return read_state(json_path, seed, sys.modules[__name__])


def rewrite_seed(html_path, html, doc, rev):
    new = SEED_RE.sub(lambda m: m.group(1) + seed_text(doc, rev) + m.group(3), html, count=1)
    if new != html and html_path.read_text(encoding="utf-8") == html:
        atomic_write(html_path, new)


def gated(gate, doc, op, ctx):
    return gate.apply(doc, op, ctx, apply_op) if gate else apply_op(doc, op, ctx)


def sync(slug, changes=None, ops=None, gate=None):
    from scripts.swarm_ledger.repository import FileLedgerRepository

    return FileLedgerRepository(sys.modules[__name__]).apply_ops(slug, changes=changes, ops=ops, gate=gate)
