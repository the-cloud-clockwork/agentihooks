"""Queue rank of a swarm task: the tick claims eligible tasks highest rank first (see scripts.swarm.claim_order).

Distinct from the Priorities panel, which holds operator decisions. `next` is an alias that stores urgent.
"""

import re

from scripts.swarm_ledger import ports

RANKS = ("urgent", "high", "normal", "low")
ALIASES = {"next": "urgent"}
SPOKEN = {"urgency": "urgent", "urgently": "urgent"}
DEFAULT = "normal"
GENERATED_PROFILE = "documentation"
OPERATOR = "operator"
QUOTE_WORDS = 3
OPS = ("task_rank",)
ITEM_RE = re.compile(r"^tasks/[^/]+$")
HELD = "; move it to a phase that admits it or lift the hold first"


def canonical(value) -> str:
    rank = ALIASES.get(value, value) if isinstance(value, str) else None
    if rank not in RANKS:
        raise ValueError(f"rank must be one of {RANKS}, or next for urgent")
    return rank


def order(task: dict) -> int:
    rank = task.get("rank")
    if rank not in RANKS:
        rank = RANKS[-1] if generated(task) else DEFAULT
    return RANKS.index(rank)


def generated(task: dict) -> bool:
    return task.get("profile") == GENERATED_PROFILE and task.get("follow_up") is True


def check(op):
    by = op.get("by", OPERATOR)
    shape = set(op) - {"by", "if_owned"} == {"op", "id", "item", "rank"} and op.get("if_owned", True) is True
    if not shape or not ITEM_RE.match(str(op["item"])) or not _named(by):
        raise ValueError("task_rank takes an id, an item tasks/<id>, a rank and an optional author by and if_owned")
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
    rank, path = canonical(op["rank"]), f"{op['item']}/rank"
    if by != OPERATOR and (refusal := pinned(task_id, author(ctx, path) if "rank" in task else None)):
        ctx.refused.append(refusal)
        return False
    if refusal := urgent_refusal({**task, "rank": rank}, doc):
        ctx.refused.append(refusal)
        return False
    if task.get("rank") != rank or author(ctx, path) != by:
        task["rank"] = rank
        ctx.stamp(path, by)
        ctx.record(by, "rank set", op["item"], text=rank)
        ctx.dirty = True
    return True


def author(ctx, path):
    return ctx.stamps.get(path, {}).get("by")


def pinned(task_id, owner) -> str:
    if owner != OPERATOR:
        return ""
    return f"task {task_id} has a rank the operator set: relay his words with task set {task_id} rank=<rank> --quote"


def quoted(rank, words) -> bool:
    said = set(re.findall(r"[a-z]+", words.lower()))
    names = {rank} | {word for word, target in (ALIASES | SPOKEN).items() if target == rank}
    return len(words.split()) >= QUOTE_WORDS and bool(names & said)


def urgent_refusal(task: dict, doc: dict) -> str:
    from scripts.swarm_ledger.repository import hierarchy

    claim_hold = ports.swarm().claim_hold
    if task.get("rank") != "urgent" or task.get("state", "open") != "open":
        return ""
    why = claim_hold.phased(task, doc) or claim_hold.freezes(task, doc, hierarchy.project(doc)[0])
    return f"task {task['id']} cannot be urgent: {why}{HELD}" if why else ""
