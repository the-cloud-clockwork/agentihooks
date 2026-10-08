"""Queue rank of a swarm task: the tick claims eligible tasks highest rank first (see scripts.swarm.claim_order).

Distinct from the Priorities panel, which holds operator decisions. `next` is an alias that stores urgent.
"""

import re

RANKS = ("urgent", "high", "normal", "low")
ALIASES = {"next": "urgent"}
DEFAULT = "normal"
OPS = ("task_rank",)
ITEM_RE = re.compile(r"^tasks/[^/]+$")


def canonical(value) -> str:
    rank = ALIASES.get(value, value) if isinstance(value, str) else None
    if rank not in RANKS:
        raise ValueError(f"rank must be one of {RANKS}, or next for urgent")
    return rank


def order(task: dict) -> int:
    rank = task.get("rank")
    return RANKS.index(rank if rank in RANKS else DEFAULT)


def check(op):
    if set(op) != {"op", "id", "item", "rank"} or not ITEM_RE.match(str(op["item"])):
        raise ValueError("task_rank is the operator's and takes only an id, an item tasks/<id> and a rank")
    canonical(op["rank"])


def apply(doc, op, ctx):
    task_id = op["item"].split("/")[1]
    task = next((t for t in doc.get("tasks", []) if t["id"] == task_id), None)
    if task is None:
        return False
    rank = canonical(op["rank"])
    if task.get("rank", DEFAULT) != rank:
        task["rank"] = rank
        ctx.stamp(f"{op['item']}/rank", "operator")
        ctx.record("operator", "rank set", op["item"], text=rank)
        ctx.dirty = True
    return True
