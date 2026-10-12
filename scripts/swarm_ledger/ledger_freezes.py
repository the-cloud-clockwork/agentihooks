"""Freeze and focus records on plan nodes and on lane and kind selectors."""

import re
import time

from scripts.swarm.naming import lane_of, resolve_name
from scripts.swarm_ledger.ledger_kinds import KINDS
from scripts.swarm_ledger.ledger_tasks import LANES
from scripts.swarm_ledger.repository.hierarchy import project

OPS = ("freeze_set", "freeze_clear")
VERBS = ("freeze", "focus")
NODE_RE = re.compile(r"^(plans|phases|slices|tasks)/[^/\s]+$")
SELECTORS = frozenset({*(f"lane:{lane}" for lane in LANES), *(f"kind:{kind}" for kind in KINDS)})
AUTHOR_RE = re.compile(r"^[A-Za-z][\w.@-]{0,63}$")
DISPATCH = "dispatch"
FULL = "full"
FIELDS = {"freeze_set": ("verb", "target", "reason", "quote"), "freeze_clear": ("target", "reason", "quote")}
EVENTS = {"freeze": "frozen", "focus": "focused"}
MAX_TEXT = 2000


def selector(target: str) -> bool:
    return target in SELECTORS


def check(op):
    if set(op) - {"op", "id", "by", *FIELDS[op["op"]]}:
        raise ValueError(f"{op['op']} takes only {' '.join(FIELDS[op['op']])}")
    by = op.get("by")
    if by is not None and (not isinstance(by, str) or not AUTHOR_RE.match(by) or by == "operator"):
        raise ValueError("invalid freeze author")
    if op["op"] == "freeze_set" and op.get("verb") not in VERBS:
        raise ValueError("freeze_set needs verb freeze or focus")
    target = op.get("target")
    if not isinstance(target, str) or not (NODE_RE.match(target) or selector(target)):
        raise ValueError("a freeze target is a plan, phase, slice or task address")
    if any(not isinstance(op.get(key, ""), str) or len(op.get(key, "")) > MAX_TEXT for key in ("reason", "quote")):
        raise ValueError("reason and quote must be text")


def autonomy(slug: str) -> str:
    from redis import RedisError

    from scripts.swarm.store import SwarmError, connect

    try:
        return connect().config(slug).autonomy
    except (SwarmError, RedisError):
        return ""


def author(op, ctx) -> tuple[str, str] | None:
    """(writer, the operator's quoted words) when the op may write freezes, else None."""
    import ledger_relay

    by = op.get("by")
    if by is None:
        return "operator", op.get("quote")
    if lane_of(resolve_name(by)) == DISPATCH:
        return (by, "") if autonomy(ctx.slug) == FULL else None
    master = ctx.meta["members"].get(by, {}).get("role") == "orchestrator"
    words = ledger_relay.verified(by, op["quote"]) if master and op.get("quote") else ""
    return (by, words) if words else None


def under(doc: dict, target: str) -> set:
    below = {}
    for node, (_, link, _) in project(doc)[0].items():
        below.setdefault(link, []).append(node)
    for task in doc.get("tasks", []):
        below.setdefault(f"tasks/{task['id']}", []).extend(f"tasks/{m}" for m in task.get("group_members", []))
    found = [target]
    for parent in found:
        found += [node for node in below.get(parent, []) if node not in found]
    return set(found)


def _set(doc, op, ctx, by, words):
    target = op["target"]
    if not selector(target) and target not in project(doc)[0]:
        return False
    rows = doc.setdefault("freezes", [])
    if any(row["verb"] == op["verb"] and row["target"] == target for row in rows):
        return True
    row = {"id": op["id"], "verb": op["verb"], "target": target, "by": by, "at": ctx.at, "reason": op.get("reason", "")}
    rows.append({**row, **({"quote": words} if words else {})})
    ctx.record(by, EVENTS[op["verb"]], target, id=op["id"], reason=row["reason"])
    ctx.stamp("freezes", by)
    return True


def _clear(doc, op, ctx, by):
    target = op["target"]
    held = under(doc, target)
    rows = doc.get("freezes", [])
    gone = [row for row in rows if row["target"] in held]
    if gone:
        doc["freezes"] = [row for row in rows if row["target"] not in held]
        cleared = [row["target"] for row in gone]
        ctx.record(by, "unfrozen", target, id=op["id"], cleared=cleared, reason=op.get("reason", ""))
        ctx.stamp("freezes", by)
    return True


def apply(doc, op, ctx):
    found = author(op, ctx)
    if found is None:
        return False
    by, words = found
    return _set(doc, op, ctx, by, words) if op["op"] == "freeze_set" else _clear(doc, op, ctx, by)


def lines(doc: dict) -> list[str]:
    return [
        f"{row['verb']}  {row['target']}  by {row['by']}  at "
        + time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(row["at"] / 1000))
        + (f"  {row['reason']}" if row.get("reason") else "")
        for row in doc.get("freezes", [])
    ]
