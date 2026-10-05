"""What agents may write in a ledger, and how an agent's status comment is kept to one per item.

Agent text is for the operator: plain words saying what was done or why it was skipped.
`check` refuses machine noise and AI-slop markers with every reason at once.
"""

import re

import ledger_link as links

LIMITS = {"comment": 50, "chat": 100, "item": 40, "priority": 20}
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


def ledger_link():
    address, number = links.address()
    host, port = re.escape(address), re.escape(str(number))
    return re.compile(rf"\bhttps?://{host}:{port}/[a-z0-9-]+\b/?")


def problems(text, kind, long=False):
    found = []
    prose = ledger_link().sub(" ", text)
    for name, pattern in RULES:
        match = pattern.search(prose)
        if match:
            found.append(f"{name} '{match.group(0).strip()}'")
    for name, mark, most in PUNCTUATION:
        if text.count(mark) > most:
            found.append(f"{text.count(mark)} {name}, at most {most}")
    words = len(text.split())
    if words > LIMITS[kind] and not (kind == "chat" and long):
        found.append(f"{words} words, at most {LIMITS[kind]}")
    return found


def check(text, kind, long=False):
    found = problems(text, kind, long)
    if found:
        raise ValueError(
            f"{kind} refused, write plain words for the operator (what was done, or why it was "
            f"skipped): {'; '.join(found)}"
        )


def can_change(entry, by, members):
    if entry.get("by") == "operator" or entry.get("deleted"):
        return False
    return entry.get("by") == by or members.get(by, {}).get("role") == "orchestrator"


def post_status(thread, by, entry_id, text, ctx, target):
    """An agent's comment amends its own latest one on the item, unless the operator spoke after it."""
    live = [e for e in thread if not e.get("deleted")]
    mine = next((e for e in reversed(live) if e.get("by") == by), None)
    replied = mine is not None and any(e.get("by") == "operator" for e in live[live.index(mine) + 1 :])
    if mine is None or replied:
        if any(e["id"] == entry_id for e in thread):
            return
        thread.append({"id": entry_id, "by": by, "at": ctx.at, "text": text})
        ctx.record(by, "comment added", target, id=entry_id, text=text)
    elif mine["text"] != text:
        from ledger_core import text_diff

        ctx.record(by, "comment edited", target, id=mine["id"], diff=text_diff(mine["text"], text))
        mine.update(text=text, edited_at=ctx.at)
    member = ctx.meta.get("members", {}).get(by)
    if member is not None:
        member["last_seen"] = ctx.at


def refused(text, kind, where, ctx):
    found = problems(text, kind)
    if found:
        ctx.refused.append(f"{where} refused: {'; '.join(found)}")
    return bool(found)


def agent_thread_op(thread, op, ctx, target, noun):
    by, text = op["by"], op.get("text", "")
    if op["op"] == "add" and noun == "comment":
        post_status(thread, by, op["id"], text, ctx, target)
        return True
    if op["op"] == "add":
        if not any(e["id"] == op["id"] for e in thread):
            thread.append({"id": op["id"], "by": by, "at": ctx.at, "text": text})
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


def audit_item(where, item):
    rows = []
    if "text" in item and problems(item["text"], "item"):
        rows.append((where, "text", "", problems(item["text"], "item")))
    counts = {}
    for entry in item.get("comments", []):
        if entry.get("deleted") or entry.get("by") == "operator":
            continue
        counts[entry["by"]] = counts.get(entry["by"], 0) + 1
        if problems(entry["text"], "comment"):
            rows.append((where, entry["id"], entry["by"], problems(entry["text"], "comment")))
    rows += [(where, "-", by, [f"{n} comments, keep one status"]) for by, n in counts.items() if n > 1]
    return rows


def audit(doc):
    """Every agent-written text the filter would refuse today, as (where, entry id, author, reasons)."""
    rows = []
    for name in ("phases", "questions", "followups"):
        for item in doc.get(name, []):
            rows += audit_item(f"{name}/{item['id']}", item)
    for entry in doc.get("chat", []):
        if not entry.get("deleted") and entry.get("by") != "operator" and problems(entry["text"], "chat"):
            rows.append(("chat", entry["id"], entry["by"], problems(entry["text"], "chat")))
    return rows
