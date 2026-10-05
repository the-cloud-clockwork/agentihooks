"""Adding a source to a ledger after it was created, such as the ledger of a Doctor linked to it."""

import re

OPS = ("source_add",)
AUTHOR_RE = re.compile(r"^[A-Za-z][\w.-]{0,63}$")
MAX_SOURCE = 1000


def check(op):
    if set(op) != {"op", "id", "by", "source"} or not isinstance(op["by"], str) or not AUTHOR_RE.match(op["by"]):
        raise ValueError("source_add takes id, by (an author name) and source")
    if not isinstance(op["source"], str) or not op["source"].strip() or len(op["source"]) > MAX_SOURCE:
        raise ValueError(f"source must be text of at most {MAX_SOURCE} characters")


def apply(doc, op, ctx):
    sources = doc.setdefault("sources", [])
    if op["source"] not in sources:
        sources.append(op["source"])
        ctx.stamp("sources", op["by"])
        ctx.record(op["by"], "source added", "sources", id=op["id"], text=op["source"])
    return True
