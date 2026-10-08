"""Priorities: one-line asks that wait on the operator, at most one per item.

Agents raise them by hand; the server derives one for every unanswered question, blocked task, pull request
awaiting merge approval and follow-up flagged for the operator, and drops it once that no longer holds.
"""

import re

import ledger_comments

ITEM_RE = re.compile(r"^(phases|questions|followups|tasks)/[^/]+$")
AUTHOR_RE = re.compile(r"^[A-Za-z][\w.@-]{0,63}$")
OPS = ("priority", "priority_clear")
DERIVED_WORDS = 16


def check(op):
    by = op.get("by")
    if by is not None and (not isinstance(by, str) or not AUTHOR_RE.match(by) or by == "operator"):
        raise ValueError("by must be an agent name")
    if op["op"] == "priority":
        if by is None:
            raise ValueError("priorities are added by agents")
        if not ITEM_RE.match(str(op.get("item"))) or not isinstance(op.get("text"), str) or not op["text"].strip():
            raise ValueError("priority needs item <list>/<id> and text")
        ledger_comments.check(op["text"], "priority")
    elif not isinstance(op.get("target"), str) or not op["target"]:
        raise ValueError("priority_clear needs target: a priority id or all")
    elif "reason" in op and (not isinstance(op["reason"], str) or not op["reason"].strip()):
        raise ValueError("priority_clear reason must be text")


def _add(doc, op, ctx):
    name, item_id = op["item"].split("/")
    item = next((i for i in doc.get(name, []) if i["id"] == item_id), None)
    if item is None:
        return False
    if item.get("out_of_scope"):
        ctx.refused.append("Cannot add a priority to an out of scope item.")
        return False
    text = op["text"].strip()
    rows = [p for p in doc.setdefault("priorities", []) if p["item"] != op["item"]]
    doc["priorities"] = rows + [{"id": op["id"], "item": op["item"], "text": text, "by": op["by"], "at": ctx.at}]
    ctx.record(op["by"], "priority added", op["item"], id=op["id"], text=text)
    return True


def _clear(doc, op, ctx):
    rows = doc.setdefault("priorities", [])
    gone = rows if op["target"] == "all" else [p for p in rows if op["target"] in (p["id"], p["item"])]
    if not gone:
        return op["target"] == "all"
    doc["priorities"] = [p for p in rows if p not in gone]
    found, dismissed = wanted(doc), ctx.meta.setdefault("priorities_dismissed", {})
    for row in gone:
        if row["item"] in found:
            dismissed[row["item"]] = found[row["item"]]
        reason = {"reason": op["reason"]} if "reason" in op else {}
        ctx.record(op.get("by", "operator"), "priority cleared", row["item"], id=row["id"], text=row["text"], **reason)
    return True


def apply(doc, op, ctx):
    return _add(doc, op, ctx) if op["op"] == "priority" else _clear(doc, op, ctx)


def _short(text):
    words = text.split()
    return " ".join(words[:DERIVED_WORDS]) + ("…" if len(words) > DERIVED_WORDS else "")


def _reason(task):
    said = [
        c["text"]
        for c in task.get("comments", [])
        if c.get("by") != "operator" and not c.get("deleted") and c.get("text", "").strip()
    ]
    return said[-1] if said else task.get("title", "")


def _task_line(task):
    if task.get("state") == "blocked":
        return "Blocked: " + _short(_reason(task))
    if task.get("state") == "pr" and task.get("awaiting") == "approval":
        return "Approve the merge: " + _short(task.get("title", ""))
    return None


def wanted(doc):
    """Item path -> line, for everything that waits on the operator now."""
    found = {}
    for q in doc.get("questions", []):
        if not q.get("out_of_scope") and not any(not a.get("deleted") for a in q.get("answers", [])):
            found[f"questions/{q['id']}"] = "Answer: " + _short(q.get("text", ""))
    for task in doc.get("tasks", []):
        line = None if task.get("out_of_scope") else _task_line(task)
        if line:
            found[f"tasks/{task['id']}"] = line
    for phase in doc["phases"]:
        review = phase.get("review") or {}
        if review.get("escalated") and review.get("state") == "sent_back" and not phase.get("out_of_scope"):
            notes = "; ".join(review["notes"])
            found[f"phases/{phase['id']}"] = f"Decide the plan, sent back {review['rounds']} times: " + _short(notes)
    for f in doc.get("followups", []):
        if f.get("needs_operator") and not f.get("done") and not f.get("out_of_scope"):
            found[f"followups/{f['id']}"] = "Decide: " + _short(f.get("text", ""))
    return found


def _derived_row(item, text, old, ctx):
    if old and old["text"] == text:
        return old
    return {
        "id": "auto-" + item.replace("/", "-"),
        "item": item,
        "text": text,
        "by": "ledger",
        "at": ctx.at,
        "derived": True,
    }


def derive(doc, ctx):
    rows = doc.setdefault("priorities", [])
    out_of_scope = {
        f"{name}/{item['id']}"
        for name in ("phases", "questions", "followups", "tasks")
        for item in doc.get(name, [])
        if item.get("out_of_scope")
    }
    manual = [p for p in rows if not p.get("derived") and p["item"] not in out_of_scope]
    for p in rows:
        if not p.get("derived") and p["item"] in out_of_scope:
            ctx.record("swarm", "priority cleared", p["item"], id=p["id"], reason="its item is out of scope")
    held, old = {p["item"] for p in manual}, {p["item"]: p for p in rows if p.get("derived")}
    found, dismissed = wanted(doc), ctx.meta.setdefault("priorities_dismissed", {})
    for item in [i for i in dismissed if found.get(i) != dismissed[i]]:
        del dismissed[item]
    derived = [
        _derived_row(item, text, old.get(item), ctx)
        for item, text in found.items()
        if item not in held and item not in dismissed
    ]
    if manual + derived != rows:
        doc["priorities"] = manual + derived
        ctx.dirty = True
