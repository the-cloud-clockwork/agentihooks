"""A ledger's size: small for one session's work without a swarm, swarm for a plan a swarm works."""

import re
from typing import Any

from scripts.swarm_ledger import ports
from scripts.swarm_ledger.op_context import OpContext

OPS = ("size_set",)
SIZES = ("small", "swarm")
AUTHOR_RE = re.compile(r"^[A-Za-z][\w.@-]{0,63}$")


def size_of(doc):
    return doc.get("size") if doc.get("size") in SIZES else "swarm"


def check(op):
    if set(op) != {"op", "id", "by", "size"} or not isinstance(op["by"], str) or not AUTHOR_RE.match(op["by"]):
        raise ValueError("size_set takes id, by (an author name) and size")
    if op["size"] not in SIZES:
        raise ValueError(f"size must be one of {', '.join(SIZES)}")


def bound_swarm(slug: str) -> str:
    store = ports.swarm().store
    swarms = store.connect()
    try:
        return swarms.config(slug).slug
    except store.SwarmError:
        return ""


def apply(doc: dict[str, Any], op: dict[str, Any], ctx: OpContext) -> bool:
    swarm = bound_swarm(ctx.slug) if op["size"] == "small" else ""
    if swarm:
        ctx.refused.append(f"size_set refused: swarm {swarm} works this ledger and a small ledger sends it no events")
        return False
    if doc.get("size") != op["size"]:
        doc["size"] = op["size"]
        ctx.stamp("size", op["by"])
        ctx.record(op["by"], "size set", "", id=op["id"], text=op["size"])
    return True
