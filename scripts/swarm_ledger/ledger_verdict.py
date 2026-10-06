"""Verdict: the operator's Approve or Deny on an item, written as his comment that carries the ask, clearing its priority.

An approval of an ask to set a condition reads "approved, set a condition", so the condition gate opens for the task.
"""

import re

import ledger_priorities

ITEM_RE = re.compile(r"^(phases|questions|followups|tasks)/[^/]+$")
OPS = ("verdict",)
VERDICTS = ("approved", "denied")
CONDITION = "set a condition"


def check(op):
    if "by" in op:
        raise ValueError("verdict is the operator's")
    if not ITEM_RE.match(str(op.get("item"))):
        raise ValueError("verdict needs item <list>/<id>")
    if op.get("verdict") not in VERDICTS:
        raise ValueError("verdict must be approved or denied")


def text(verdict, ask):
    from hooks.context.conditions import contains_condition_signal

    if verdict == "denied" or not ask:
        return verdict
    return f"{verdict}, {CONDITION if contains_condition_signal(ask) else ask}"


def apply(doc, op, ctx):
    name, item_id = op["item"].split("/")
    item = next((i for i in doc[name] if i["id"] == item_id and not i.get("deleted")), None)
    if item is None:
        return False
    if any(e["id"] == op["id"] for e in item["comments"]):
        return True
    ask = next((p["text"] for p in doc.get("priorities", []) if p["item"] == op["item"]), "")
    said = text(op["verdict"], ask)
    item["comments"].append({"id": op["id"], "by": "operator", "at": ctx.at, "text": said})
    ctx.record("operator", "comment added", op["item"], id=op["id"], text=said)
    ctx.stamp(f"{op['item']}/comments", "operator")
    ledger_priorities.apply(doc, {"op": ledger_priorities.OPS[1], "target": op["item"]}, ctx)
    return True
