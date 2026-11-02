"""Freeze and focus records on plan nodes and on lane and kind selectors."""

import re
import time
from typing import Any

from scripts.swarm_ledger import ports
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
FIELDS = {
    "freeze_set": ("verb", "target", "reason", "quote", "position", "only"),
    "freeze_clear": ("target", "reason", "quote"),
}
POSITIONS = ("top", "bottom")
EVENTS = {"freeze": "frozen", "focus": "focused"}
MAX_TEXT = 2000


def resolve_name(name):
    return ports.swarm().naming.resolve_name(name)


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
    if (op.get("position") or op.get("only")) and op.get("verb") != "focus":
        raise ValueError("position and only apply to a focus")
    if op.get("position", "bottom") not in POSITIONS or not isinstance(op.get("only", False), bool):
        raise ValueError("position is top or bottom and only is true or false")
    target = op.get("target")
    if not isinstance(target, str) or not (NODE_RE.match(target) or selector(target)):
        raise ValueError("a freeze target is a plan, phase, slice or task address")
    if any(not isinstance(op.get(key, ""), str) or len(op.get(key, "")) > MAX_TEXT for key in ("reason", "quote")):
        raise ValueError("reason and quote must be text")


def autonomy(slug: str) -> str:
    from redis import RedisError

    SwarmError, connect = ports.swarm().store.SwarmError, ports.swarm().store.connect

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
    if ports.swarm().naming.lane_of(resolve_name(by)) == DISPATCH:
        if autonomy(ctx.slug) == FULL:
            return by, ""
        ctx.refused.append(f"{op['op']} refused: dispatcher {by} requires full autonomy")
        return None
    if ctx.meta["members"].get(by, {}).get("role") != "orchestrator":
        ctx.refused.append(f"{op['op']} refused: {by} is not an orchestrator of this ledger")
        return None
    words = ledger_relay.verified(by, op["quote"]) if op.get("quote") else ""
    return by, words


def _set(doc: dict[str, Any], op: dict[str, Any], ctx: Any, by: str, words: str) -> bool:
    target: str = op["target"]
    graph = project(doc)[0]
    if not selector(target) and target not in graph:
        ctx.refused.append(f"{op['op']} refused: target {target} does not exist")
        return False
    verb: str = op["verb"]
    rows: list[dict[str, Any]] = doc.setdefault("freezes", [])
    kept = [row for row in rows if not _displaced(row, op, graph)]
    present = any(row["verb"] == verb and row["target"] == target for row in rows)
    top = op.get("position") == "top"
    if kept == rows and present and not op.get("position"):
        return True
    kept = [row for row in kept if not (row["verb"] == verb and row["target"] == target)]
    row: dict[str, Any] = {"id": op["id"], "verb": verb, "target": target, "by": by, "at": ctx.at}
    row = {**row, "reason": op.get("reason", ""), **({"quote": words} if words else {})}
    at = next((i for i, r in enumerate(kept) if r["verb"] == "focus"), len(kept)) if top else len(kept)
    doc["freezes"] = [*kept[:at], row, *kept[at:]]
    ctx.record(by, EVENTS[verb], target, id=op["id"], reason=row["reason"])
    ctx.stamp("freezes", by)
    return True


def _displaced(row: dict[str, Any], op: dict[str, Any], graph: dict[str, Any]) -> bool:
    """A focus drops the freezes on its target and beneath it, and with only every other focus; a freeze drops the
    focus on its own target."""
    target: str = op["target"]
    if op["verb"] == "freeze":
        return row["verb"] == "focus" and row["target"] == target
    if row["verb"] == "focus":
        return op.get("only") is True and row["target"] != target
    return row["target"] == target or target in _above(row["target"], graph)


def _above(node: str, graph: dict[str, Any]) -> list[str]:
    chain: list[str] = []
    up: str | None = (graph.get(node) or (None, None))[1]
    while up and up not in chain:
        chain.append(up)
        up = (graph.get(up) or (None, None))[1]
    return chain


def _clear(doc, op, ctx, by):
    target = op["target"]
    rows = doc.get("freezes", [])
    gone = [row for row in rows if row["target"] == target]
    if gone:
        doc["freezes"] = [row for row in rows if row["target"] != target]
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
