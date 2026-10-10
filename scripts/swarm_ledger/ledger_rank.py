"""Queue rank of a swarm task: the tick claims eligible tasks highest rank first (see scripts.swarm.claim_order).

Distinct from the Priorities panel, which holds operator decisions. `next` is an alias that stores urgent.
"""

import re

RANKS = ("urgent", "high", "normal", "low")
ALIASES = {"next": "urgent"}
DEFAULT = "normal"
OPERATOR = "operator"
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
    by = op.get("by", OPERATOR)
    shape = set(op) - {"by", "if_unranked"} == {"op", "id", "item", "rank"} and op.get("if_unranked", True) is True
    if not shape or not ITEM_RE.match(str(op["item"])) or not _named(by):
        raise ValueError("task_rank takes an id, an item tasks/<id>, a rank and an optional author by and if_unranked")
    canonical(op["rank"])


def _named(by) -> bool:
    return isinstance(by, str) and bool(by)


def apply(doc, op, ctx):
    from scripts.swarm_ledger.ledger_tasks import rank_refusal

    task_id = op["item"].split("/")[1]
    task = next((t for t in doc.get("tasks", []) if t["id"] == task_id), None)
    if task is None:
        return False
    by = op.get("by", OPERATOR)
    if refusal := rank_refusal(by):
        ctx.refused.append(refusal)
        return False
    rank = canonical(op["rank"])
    if op.get("if_unranked") and "rank" in task:
        return True
    if task.get("rank") != rank:
        task["rank"] = rank
        ctx.stamp(f"{op['item']}/rank", by)
        ctx.record(by, "rank set", op["item"], text=rank)
        ctx.dirty = True
    return True
