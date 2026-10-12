from scripts.swarm_ledger import ledger_kinds, plan_ranges
from scripts.swarm_ledger.ledger_groups import AUTHOR_RE, ITEM_RE, REFUSED_LANES

OPS = ("task_split",)
MIN_HALVES, MAX_HALVES = 2, 4
SPLIT_KINDS = ("code", "ci")
FIELDS = {"op", "id", "by", "item", "halves"}


def check(op):
    by = op.get("by")
    if not isinstance(by, str) or not AUTHOR_RE.match(by) or by == "operator":
        raise ValueError("task_split by: an agent name")
    if set(op) != FIELDS or not ITEM_RE.match(str(op["item"])):
        raise ValueError("task_split fields: id, by, item tasks/<id>, halves")
    halves = op["halves"]
    if not isinstance(halves, list) or not MIN_HALVES <= len(halves) <= MAX_HALVES:
        raise ValueError(f"halves: {MIN_HALVES} to {MAX_HALVES}")
    try:
        ranges = [plan_ranges.bounds(half) for half in halves]
    except ValueError:
        raise ValueError("halves: plan line ranges") from None
    if any(after[0] <= before[1] for before, after in zip(ranges, ranges[1:])):
        raise ValueError("halves: ordered, not overlapping")


def refusal(task: dict) -> str:
    if ledger_kinds.kind(task) not in SPLIT_KINDS:
        return "not a code or ci task"
    if task.get("split_pulls"):
        return "a half has a pull request"
    return ""


def apply(doc, op, ctx):
    from scripts.swarm.naming import lane_of

    if lane_of(op["by"]) in REFUSED_LANES:
        ctx.refused.append(f"{op['by']}: {lane_of(op['by'])} lane, no split")
        return False
    task = next((t for t in doc["tasks"] if t["id"] == op["item"].split("/")[1]), None)
    if task is None:
        return False
    if reason := refusal(task):
        ctx.refused.append(f"{op['item']}: {reason}")
        return False
    task["split"] = list(op["halves"])
    ctx.stamp(f"{op['item']}/split", op["by"])
    ctx.record(op["by"], "split", op["item"], text=", ".join(op["halves"]))
    return True
