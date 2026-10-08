"""Grouped tasks: a lead task whose agent delivers its members' work in one pull request.

The lead lists `group_members`; each member carries `merged_into` and leaves the claim queue.
"""

import re

from scripts.swarm_ledger import ledger_kinds

OPS = ("task_group",)
MAX_TASKS = 5
CEILING = "M"
MINUTES = {"S": 10, "M": 25, "L": 40}
GROUPED_KINDS = ("code", "ci")
WORKER_LANES = ("eng", "ci")
AUTHOR_RE = re.compile(r"^[A-Za-z][\w.@-]{0,63}$")
ID_RE = re.compile(r"^[A-Za-z0-9][\w.-]{0,63}$")
ITEM_RE = re.compile(r"^tasks/[^/]+$")


def check(op):
    by = op.get("by")
    if not isinstance(by, str) or not AUTHOR_RE.match(by) or by == "operator":
        raise ValueError("task_group needs `by`, an agent name other than operator")
    if set(op) != {"op", "id", "by", "item", "members"} or not ITEM_RE.match(str(op["item"])):
        raise ValueError("task_group takes only an id, by, an item tasks/<lead id> and members")
    members, lead = op["members"], op["item"].split("/")[1]
    if (
        not isinstance(members, list)
        or not members
        or not all(isinstance(m, str) and ID_RE.match(m) for m in members)
        or len(set(members)) != len(members)
        or lead in members
    ):
        raise ValueError("members must list distinct task ids other than the lead")
    if len(members) + 1 > MAX_TASKS:
        raise ValueError(f"a group holds at most {MAX_TASKS} tasks")


def refusal(tasks: list[dict], known: dict) -> str:
    """Why these tasks cannot form one group, or '' when they can."""
    if len({(t.get("lane"), t.get("profile") or "", ledger_kinds.kind(t)) for t in tasks}) > 1:
        return "grouped tasks share the same lane, profile and kind"
    for t in tasks:
        if t.get("state", "open") != "open" or t.get("claimed_by"):
            return f"task {t['id']} is not open and unclaimed"
        if t.get("merged_into") or t.get("group_members"):
            return f"task {t['id']} is already grouped"
        if ledger_kinds.kind(t) not in GROUPED_KINDS:
            return f"task {t['id']} is not a code or ci task"
        if t.get("difficulty") not in MINUTES:
            return f"task {t['id']} needs a difficulty first"
    ids = {t["id"] for t in tasks}
    for t in tasks:
        if reached := _upstream(t, known) & ids:
            return f"task {t['id']} depends on task {sorted(reached)[0]}"
        if waiting := sorted(d for d in t.get("depends_on") or [] if known.get(d, {}).get("state") != "done"):
            return f"task {t['id']} waits on task {waiting[0]}"
    if len(tasks) > MAX_TASKS:
        return f"a group holds at most {MAX_TASKS} tasks"
    sizes = [t["difficulty"] for t in tasks]
    if any(s != "S" for s in sizes) and sum(MINUTES[s] for s in sizes) > MINUTES[CEILING]:
        return f"together they pass {CEILING}"
    return ""


def _upstream(task, known):
    seen, todo = set(), list(task.get("depends_on") or [])
    while todo:
        dep = todo.pop()
        if dep not in seen:
            seen.add(dep)
            todo += known.get(dep, {}).get("depends_on") or []
    return seen


def apply(doc, op, ctx):
    from scripts.swarm.naming import lane_of

    lead_id = op["item"].split("/")[1]
    known = {t["id"]: t for t in doc.get("tasks", [])}
    if not {lead_id, *op["members"]} <= set(known):
        return False
    if lane_of(op["by"]) in WORKER_LANES:
        ctx.refused.append(f"{op['by']} works in the {lane_of(op['by'])} lane and cannot set a task group")
        return False
    group = [known[lead_id], *(known[m] for m in op["members"])]
    if reason := refusal(group, known):
        ctx.refused.append(f"{op['item']} cannot lead this group: {reason}")
        return False
    known[lead_id]["group_members"] = list(op["members"])
    ctx.stamp(f"{op['item']}/group_members", op["by"])
    for member in op["members"]:
        known[member]["merged_into"] = lead_id
        ctx.stamp(f"tasks/{member}/merged_into", op["by"])
    ctx.record(op["by"], "grouped", op["item"], text=", ".join(op["members"]))
    ctx.dirty = True
    return True
