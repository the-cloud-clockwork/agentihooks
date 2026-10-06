"""Relay: an agent posts a decision the operator gave it in its own pane, as the operator's entry.

A question takes it as an answer, any other item as a comment; the entry names the relaying agent,
the pane it came from and the operator's quoted words the relay was verified against.
"""

import re

ITEM_RE = re.compile(r"^(phases|questions|followups|tasks)/[^/]+$")
AUTHOR_RE = re.compile(r"^[A-Za-z][\w.@-]{0,63}$")
OPS = ("relay",)
RELAYED_FROM = "master pane"
MAX_TEXT = 20000


def check(op):
    by = op.get("by")
    if not isinstance(by, str) or not AUTHOR_RE.match(by) or by == "operator":
        raise ValueError("relay needs by, the relaying agent's name")
    if not ITEM_RE.match(str(op.get("item"))):
        raise ValueError("relay needs item <list>/<id>")
    for field in ("text", "quote"):
        value = op.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT:
            raise ValueError(f"relay needs {field} up to {MAX_TEXT} characters")


def apply(doc, op, ctx):
    name, item_id = op["item"].split("/")
    item = next((i for i in doc.get(name, []) if i["id"] == item_id), None)
    if item is None:
        return False
    thread = "answers" if name == "questions" else "comments"
    if any(e["id"] == op["id"] for e in item.setdefault(thread, [])):
        return True
    marks = {"relayed_by": op["by"], "relayed_from": RELAYED_FROM, "quote": op["quote"].strip()}
    text = op["text"].strip()
    item[thread].append({"id": op["id"], "by": "operator", "at": ctx.at, "text": text, **marks})
    noun = "answer" if thread == "answers" else "comment"
    ctx.record("operator", f"{noun} added", op["item"], id=op["id"], text=text, **marks)
    ctx.stamp(f"{op['item']}/{thread}", "operator")
    return True
