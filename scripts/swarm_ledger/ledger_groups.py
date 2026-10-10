"""Grouped tasks: a lead task whose agent delivers its members' work in one pull request.

The lead lists `group_members`; each member carries `merged_into` and leaves the claim queue.
"""

import re

from scripts.swarm_ledger import ledger_kinds

OPS = ("task_group", "task_ungroup")
MAX_TASKS = 5
CEILING = "M"
MINUTES = {"S": 10, "M": 25, "L": 40}
GROUPED_KINDS = ("code", "ci")
REFUSED_LANES = ("eng", "ci", "plan")
AUTHOR_RE = re.compile(r"^[A-Za-z][\w.@-]{0,63}$")
ID_RE = re.compile(r"^[A-Za-z0-9][\w.-]{0,63}$")
ITEM_RE = re.compile(r"^tasks/[^/]+$")


def check(op):
    by = op.get("by")
    if not isinstance(by, str) or not AUTHOR_RE.match(by) or by == "operator":
        raise ValueError(f"{op['op']} needs `by`, an agent name other than operator")
    if op["op"] == "task_ungroup":
        if set(op) != {"op", "id", "by", "item"} or not ITEM_RE.match(str(op["item"])):
            raise ValueError("task_ungroup takes only an id, by and an item tasks/<lead id>")
        return
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
        if t["state"] != "open" or t.get("claimed_by"):
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
    seen = list(dict.fromkeys(task.get("depends_on") or []))
    for dep in seen:
        seen += [d for d in known.get(dep, {}).get("depends_on") or [] if d not in seen]
    return set(seen)


def apply(doc, op, ctx):
    from scripts.swarm.naming import lane_of

    if lane_of(op["by"]) in REFUSED_LANES:
        verb = "release" if op["op"] == "task_ungroup" else "set"
        ctx.refused.append(f"{op['by']} works in the {lane_of(op['by'])} lane and cannot {verb} a task group")
        return False
    known = {t["id"]: t for t in doc["tasks"]}
    return (_ungroup if op["op"] == "task_ungroup" else _group)(known, op, ctx)


def _group(known, op, ctx):
    lead_id = op["item"].split("/")[1]
    if any(task_id not in known for task_id in (lead_id, *op["members"])):
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
    return True


def _ungroup(known, op, ctx):
    lead_id = op["item"].split("/")[1]
    if not known.get(lead_id, {}).get("group_members"):
        ctx.refused.append(f"{op['item']} leads no group")
        return False
    released = [m for m in known[lead_id].pop("group_members") if known.get(m, {}).get("merged_into") == lead_id]
    ctx.stamp(f"{op['item']}/group_members", op["by"])
    for member in released:
        del known[member]["merged_into"]
        ctx.stamp(f"tasks/{member}/merged_into", op["by"])
    ctx.record(op["by"], "ungrouped", op["item"], text=", ".join(released))
    return True
