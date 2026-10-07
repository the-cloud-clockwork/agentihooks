"""Answer: the swarm master answers an agent's question as itself, so it leaves the operator's Priorities.

Only the ledger's orchestrator answers; the command allows it at delegate and full autonomy, where the master
decides for the operator.
"""

import re

import ledger_comments

ITEM_RE = re.compile(r"^questions/[^/]+$")
AUTHOR_RE = re.compile(r"^[A-Za-z][\w.@-]{0,63}$")
OPS = ("answer",)
AUTONOMY = ("delegate", "full")
MAX_TEXT = 20000


def refusal(autonomy):
    if autonomy in AUTONOMY:
        return ""
    if not autonomy:
        return "answer refused: this ledger has no swarm, so the operator answers questions"
    return f"answer refused: at {autonomy} autonomy the operator answers questions; raise it with priority add"


def check(op):
    by = op.get("by")
    if not isinstance(by, str) or not AUTHOR_RE.match(by) or by == "operator":
        raise ValueError("answer needs by, the master's name")
    if not ITEM_RE.match(str(op.get("item"))):
        raise ValueError("answer needs item questions/<id>")
    text = op.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        raise ValueError(f"answer needs text up to {MAX_TEXT} characters")
    ledger_comments.check(text, "comment")


def apply(doc, op, ctx):
    question = next((q for q in doc["questions"] if q["id"] == op["item"].split("/")[1]), None)
    if question is None:
        return False
    if any(e["id"] == op["id"] for e in question["answers"]):
        return True
    if ctx.meta["members"].get(op["by"], {}).get("role") != "orchestrator":
        return False
    text = op["text"].strip()
    question["answers"].append({"id": op["id"], "by": op["by"], "at": ctx.at, "text": text})
    ctx.record(op["by"], "answer added", op["item"], id=op["id"], text=text)
    ctx.stamp(f"{op['item']}/answers", op["by"])
    return True
