"""Relay: the master posts a decision the operator gave it in its own pane, as the operator's entry.

A question takes it as an answer, any other item as a comment. Only the ledger's orchestrator may relay,
and only words the hooks recorded from the operator in its session; the entry carries those words.
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


def verified(by, quote):
    """The operator's recorded words holding the quote, under any address of the relaying agent."""
    from hooks.context import operator_words
    from scripts.swarm.naming import addresses

    return next((w for name in addresses(by) if (w := operator_words.matching(name, quote))), "")


def apply(doc, op, ctx):
    name, item_id = op["item"].split("/")
    item = next((i for i in doc[name] if i["id"] == item_id), None)
    if item is None:
        return False
    thread = "answers" if name == "questions" else "comments"
    if any(e["id"] == op["id"] for e in item[thread]):
        return True
    master = ctx.meta["members"].get(op["by"], {}).get("role") == "orchestrator"
    words = verified(op["by"], op["quote"]) if master else ""
    if not words:
        return False
    marks = {"relayed_by": op["by"], "relayed_from": RELAYED_FROM, "quote": words}
    text = op["text"].strip()
    item[thread].append({"id": op["id"], "by": "operator", "at": ctx.at, "text": text, **marks})
    noun = "answer" if thread == "answers" else "comment"
    ctx.record("operator", f"{noun} added", op["item"], id=op["id"], text=text, **marks)
    ctx.stamp(f"{op['item']}/{thread}", "operator")
    return True
