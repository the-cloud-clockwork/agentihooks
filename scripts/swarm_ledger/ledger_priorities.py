"""Priorities: one-line asks that wait on the operator's answer, at most one per item."""

import re

import ledger_comments

ITEM_RE = re.compile(r"^(phases|questions|followups)/[^/]+$")
AUTHOR_RE = re.compile(r"^[A-Za-z][\w.-]{0,63}$")
OPS = ("priority", "priority_clear")


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


def _add(doc, op, ctx):
    name, item_id = op["item"].split("/")
    if not any(i["id"] == item_id for i in doc.get(name, [])):
        return False
    text = op["text"].strip()
    rows = [p for p in doc.setdefault("priorities", []) if p["item"] != op["item"]]
    doc["priorities"] = rows + [{"id": op["id"], "item": op["item"], "text": text, "by": op["by"], "at": ctx.at}]
    ctx.record(op["by"], "priority added", op["item"], id=op["id"], text=text)
    return True


def _clear(doc, op, ctx):
    rows = doc.setdefault("priorities", [])
    gone = rows if op["target"] == "all" else [p for p in rows if p["id"] == op["target"]]
    if not gone:
        return op["target"] == "all"
    doc["priorities"] = [p for p in rows if p not in gone]
    for row in gone:
        ctx.record(op.get("by", "operator"), "priority cleared", row["item"], id=row["id"], text=row["text"])
    return True


def apply(doc, op, ctx):
    return _add(doc, op, ctx) if op["op"] == "priority" else _clear(doc, op, ctx)
