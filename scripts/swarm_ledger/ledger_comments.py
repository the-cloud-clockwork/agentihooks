"""What agents may write in a ledger, and how an agent's status comment is kept to one per item.

Agent text is for the operator: plain words saying what was done or why it was skipped.
`check` refuses machine noise and AI-slop markers with every reason at once.
"""

import re

LIMITS = {"comment": 50, "chat": 100, "item": 40, "priority": 20}
OUTCOMES = ("done", "blocked")
UNADDRESSED = (
    "chat is the operator's conversation: send it to the operator or answer his line, "
    "and talk to agents through the inbox"
)
RULES = (
    ("clock time", re.compile(r"\b\d{1,2}:[\dx]{2}(?::\d{2})?(?:\.\d+)?\s?(?:Z|UTC)?\b", re.I)),
    ("date", re.compile(r"\b\d{4}-\d{2}-\d{2}\b")),
    ("commit hash", re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")),
    ("run or job id", re.compile(r"\b\d{8,}\b")),
    (
        "file name or path",
        re.compile(
            r"\b[\w-]+\.(?:py|tsx?|jsx?|json|ya?ml|md|sh|toml|cfg|ini|html|css|txt|csv)\b"
            r"|\b[\w.-]+/[\w.-]+/"
        ),
    ),
    ("code identifier", re.compile(r"\b[a-z][a-z0-9]*_[a-z0-9_]+\b|\b[a-z]{3,}\.[a-z_]{3,}\b|\b[a-z]+[A-Z]\w*\(")),
    ("label in capitals", re.compile(r"^\s*[A-Z][A-Z ]{2,}:|\b[A-Z]{2,}(?:[ ,/]+[A-Z]{2,})+\b")),
    ("dash", re.compile(r"[—–]")),
    ("arrow", re.compile(r"→|⇒|->|=>")),
    (
        "AI phrasing",
        re.compile(
            r"\b(?:delve|seamless(?:ly)?|robust|leverag\w*|comprehensive|crucial|pivotal|utili[sz]e"
            r"|furthermore|moreover|notably|holistic|streamline\w*|facilitat\w*|underscor\w*|showcas\w*"
            r"|meticulous\w*|paramount|tapestry|game[- ]changer|cutting[- ]edge|it'?s worth noting"
            r"|it is worth noting|in summary|in conclusion|key takeaway|rest assured|let me know"
            r"|i hope this helps|great question|additionally|importantly)\b",
            re.I,
        ),
    ),
)
PUNCTUATION = (("parentheses", "(", 1), ("semicolons", ";", 1))


def web_links():
    return re.compile(r"""\bhttps?://[^\s<>"']+""", re.I)


def problems(text, kind, long=False, task_ids=()):
    found = []
    prose = web_links().sub(" ", text)
    hash_prose = re.sub(r"[\w-]+", lambda match: " " if match.group() in task_ids else match.group(), prose)
    for name, pattern in RULES:
        match = pattern.search(hash_prose if name == "commit hash" else prose)
        if match:
            found.append(f"{name} '{match.group(0).strip()}'")
    for name, mark, most in PUNCTUATION:
        if text.count(mark) > most:
            found.append(f"{text.count(mark)} {name}, at most {most}")
    words = len(text.split())
    if words > LIMITS[kind] and not (kind == "chat" and long):
        found.append(f"{words} words, at most {LIMITS[kind]}")
    return found


def check(text, kind, long=False, task_ids=()):
    found = problems(text, kind, long, task_ids)
    if found:
        raise ValueError(
            f"{kind} refused, write plain words for the operator (what was done, or why it was "
            f"skipped): {'; '.join(found)}"
        )


def can_change(entry, by, members):
    if entry.get("by") == "operator" or entry.get("deleted"):
        return False
    return entry.get("by") == by or members.get(by, {}).get("role") == "orchestrator"


def post_status(thread, by, entry_id, text, ctx, target, attachments=None):
    """An agent's comment amends its own latest one on the item, unless the operator spoke after it."""
    live = [e for e in thread if not e.get("deleted")]
    mine = next((e for e in reversed(live) if e.get("by") == by), None)
    replied = mine is not None and any(e.get("by") == "operator" for e in live[live.index(mine) + 1 :])
    if mine is None or replied or "outcome" in mine:
        if any(e["id"] == entry_id for e in thread):
            return
        thread.append({"id": entry_id, "by": by, "at": ctx.at, "text": text})
        if attachments:
            thread[-1]["attachments"] = attachments
        ctx.record(by, "comment added", target, id=entry_id, text=text)
    elif mine["text"] != text or (attachments is not None and mine.get("attachments") != attachments):
        from ledger_core import text_diff

        ctx.record(by, "comment edited", target, id=mine["id"], diff=text_diff(mine["text"], text))
        mine.update(text=text, edited_at=ctx.at)
        if attachments is not None:
            mine["attachments"] = attachments
    seen(ctx, by)


def post_outcome(thread, op, ctx, target):
    if not any(e["id"] == op["id"] for e in thread):
        thread.append({"id": op["id"], "by": op["by"], "at": ctx.at, "text": op["text"], "outcome": op["outcome"]})
        ctx.record(op["by"], "comment added", target, id=op["id"], text=op["text"])
    seen(ctx, op["by"])


def seen(ctx, by):
    member = ctx.meta.get("members", {}).get(by)
    if member is not None:
        member["last_seen"] = ctx.at


def check_outcome(op):
    if "outcome" not in op:
        return
    agent_comment = op["op"] == "add" and "by" in op and op["thread"].endswith("/comments")
    if op["outcome"] not in OUTCOMES or not agent_comment or "attachments" in op:
        raise ValueError(
            "outcome rides only on an agent add to a comment thread, as done or blocked, without attachments"
        )


def refused(text, kind, where, ctx):
    found = problems(text, kind)
    if found:
        ctx.refused.append(f"{where} refused: {'; '.join(found)}")
    return bool(found)


def addressed(thread, op):
    if op.get("to") == "operator":
        return True
    return any(e["id"] == op.get("reply_to") and e.get("by") == "operator" and not e.get("deleted") for e in thread)


def agent_thread_op(thread, op, ctx, target, noun):
    by, text = op["by"], op.get("text", "")
    if op.get("attachments") and by not in ctx.meta.get("members", {}):
        return False
    if op["op"] == "add" and "outcome" in op:
        post_outcome(thread, op, ctx, target)
        return True
    if op["op"] == "add" and noun == "comment":
        post_status(thread, by, op["id"], text, ctx, target, op.get("attachments"))
        return True
    if op["op"] == "add":
        if noun == "message" and not addressed(thread, op):
            ctx.refused.append(f"{by} message refused: {UNADDRESSED}")
            return False
        if not any(e["id"] == op["id"] for e in thread):
            from ledger_core import new_entry

            thread.append(new_entry(op, by, ctx.at, text))
            if op.get("attachments"):
                thread[-1]["attachments"] = op["attachments"]
            ctx.record(by, f"{noun} added", target, id=op["id"], text=text)
        return True
    entry = next((e for e in thread if e["id"] == op["id"]), None)
    if entry is None or not can_change(entry, by, ctx.meta.get("members", {})):
        return False
    from ledger_core import text_diff

    if op["op"] == "edit" and text != entry["text"]:
        ctx.record(by, f"{noun} edited", target, id=entry["id"], diff=text_diff(entry["text"], text))
        entry.update(text=text, edited_at=ctx.at)
    elif op["op"] == "delete":
        ctx.record(by, f"{noun} deleted", target, id=entry["id"], text=entry["text"])
        entry.update(text="", deleted=True, edited_at=ctx.at)
    return True


def audit_item(where, item, task_ids=()):
    rows = []
    if "text" in item and problems(item["text"], "item", task_ids=task_ids):
        rows.append((where, "text", "", problems(item["text"], "item", task_ids=task_ids)))
    counts = {}
    for entry in item.get("comments", []):
        if entry.get("deleted") or entry.get("by") == "operator":
            continue
        counts[entry["by"]] = counts.get(entry["by"], 0) + 1
        if problems(entry["text"], "comment", task_ids=task_ids):
            rows.append((where, entry["id"], entry["by"], problems(entry["text"], "comment", task_ids=task_ids)))
    rows += [(where, "-", by, [f"{n} comments, keep one status"]) for by, n in counts.items() if n > 1]
    return rows


def audit(doc):
    """Every agent-written text the filter would refuse today, as (where, entry id, author, reasons)."""
    rows = []
    task_ids = tuple(task["id"] for task in doc.get("tasks", []))
    for name in ("phases", "questions", "followups"):
        for item in doc.get(name, []):
            rows += audit_item(f"{name}/{item['id']}", item, task_ids)
    for entry in doc.get("chat", []):
        if (
            not entry.get("deleted")
            and entry.get("by") != "operator"
            and problems(entry["text"], "chat", task_ids=task_ids)
        ):
            rows.append(("chat", entry["id"], entry["by"], problems(entry["text"], "chat", task_ids=task_ids)))
    return rows
