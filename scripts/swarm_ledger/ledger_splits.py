from scripts.swarm_ledger import ledger_kinds, ports
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
    ranges = _ranges(halves)
    if ranges is None:
        if not all(isinstance(half, str) and half and _ranges([half]) is None for half in halves):
            raise ValueError("halves: plan line ranges")
        if len(set(halves)) != len(halves):
            raise ValueError("halves: distinct labels")
    elif any(after[0] <= before[1] for before, after in zip(ranges, ranges[1:])):
        raise ValueError("halves: ordered, not overlapping")


def _ranges(halves):
    from scripts.swarm_ledger import plan_ranges

    try:
        return [plan_ranges.bounds(half) for half in halves]
    except ValueError:
        return None


def ranged(halves: list) -> bool:
    return _ranges(halves) is not None


def refusal(task: dict, halves: list) -> str:
    from scripts.swarm_ledger import plan_ranges

    if ledger_kinds.kind(task) not in SPLIT_KINDS:
        return "not a code or ci task"
    if task.get("split_pulls"):
        return "a half has a pull request"
    if task.get("pr_url"):
        return "the task has a pull request outside the split"
    lines = task.get("plan_lines")
    if not ranged(halves):
        return "halves: plan line ranges" if lines or task.get("plan_slice") else ""
    if lines and not _covers(plan_ranges.bounds(lines), [plan_ranges.bounds(half) for half in halves]):
        return f"halves: cover plan lines {lines}"
    return ""


def _covers(whole, ranges):
    edges = [whole[0] - 1, *(edge for start, end in ranges for edge in (start - 1, end)), whole[1]]
    return all(before == after for before, after in zip(edges[::2], edges[1::2]))


def apply(doc, op, ctx):
    lane_of = ports.swarm().naming.lane_of

    if lane_of(op["by"]) in REFUSED_LANES:
        ctx.refused.append(f"{op['by']}: {lane_of(op['by'])} lane, no split")
        return False
    task = next((t for t in doc["tasks"] if t["id"] == op["item"].split("/")[1]), None)
    if task is None:
        return False
    if reason := refusal(task, op["halves"]):
        ctx.refused.append(f"{op['item']}: {reason}")
        return False
    task["split"] = list(op["halves"])
    ctx.stamp(f"{op['item']}/split", op["by"])
    ctx.record(op["by"], "split", op["item"], text=", ".join(op["halves"]))
    return True
