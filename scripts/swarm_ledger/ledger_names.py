import re

OPS = ("agent_rename",)
AUTHOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")


def check(op):
    if op.get("by") != "swarm" or any(
        not isinstance(op.get(key), str) or not AUTHOR_RE.fullmatch(op[key]) for key in ("old", "new")
    ):
        raise ValueError("agent rename needs swarm, old and new agent names")


def rename(doc, meta, old, new):
    if old == new:
        return False
    changed = False
    members = meta.setdefault("members", {})
    member = members.pop(old, None)
    if member is not None:
        current = members.get(new, {})
        members[new] = {
            **member,
            **current,
            "claims": list(dict.fromkeys(member.get("claims", []) + current.get("claims", []))),
            "handled_rev": min(member.get("handled_rev", 0), current.get("handled_rev", member.get("handled_rev", 0))),
        }
        changed = True
    for task in doc.get("tasks", []):
        if task.get("claimed_by") == old:
            task["claimed_by"] = new
            changed = True
    if doc.get("orchestrator") == old:
        doc["orchestrator"] = new
        changed = True
    return changed


def apply(doc, op, ctx):
    if rename(doc, ctx.meta, op["old"], op["new"]):
        ctx.record("swarm", "agent renamed", "crew", old=op["old"], new=op["new"])
    return True
