"""The tick's time left write: live slots and the CI median, with the calculation kept beside them."""

from typing import TYPE_CHECKING, Any

import ledger_stats

if TYPE_CHECKING:
    from scripts.swarm_ledger.ledger_core import Context

OPS = ("time_left",)
FIELDS = {"op", "id", "by", "slots", "ci_minutes"}
FOCUS = ("fix_phase", "watched")


def check(op: dict[str, Any]) -> None:
    if not FIELDS <= set(op) <= FIELDS | set(FOCUS) or op["by"] != "swarm":
        raise ValueError("time_left takes id, by swarm, slots and ci_minutes, and optionally fix_phase and watched")
    if not isinstance(op.get("fix_phase", ""), str):
        raise ValueError("fix_phase must be text")
    watched: Any = op.get("watched", [])
    if not isinstance(watched, list) or not all(isinstance(line, str) for line in watched):
        raise ValueError("watched must be a list of text")
    if op["slots"] is not None and (type(op["slots"]) is not int or op["slots"] < 0):
        raise ValueError("slots must be a nonnegative integer or null")
    ci = op["ci_minutes"]
    if ci is not None and (type(ci) not in (int, float) or ci < 0):
        raise ValueError("ci_minutes must be a nonnegative number or null")


def apply(doc: dict[str, Any], op: dict[str, Any], ctx: "Context") -> bool:
    inputs: dict[str, Any] = {
        "slots": op["slots"],
        "ci_minutes": op["ci_minutes"],
        **{key: op[key] for key in FOCUS if key in op},
    }
    meta: dict[str, Any] = ctx.meta
    own_events: list[dict[str, Any]] = getattr(ctx, "events")
    now: int = ctx.at
    calculate: Any = getattr(ledger_stats, "calculate")
    result: dict[str, Any] = calculate(doc, meta["events"] + own_events, now, inputs)
    meta["time_left"] = {"at": ctx.at, "inputs": inputs, "calculation": result}
    ctx.dirty = True
    if result["minutes"] is not None and doc.get("time_left_minutes") != result["minutes"]:
        doc["time_left_minutes"] = result["minutes"]
        stamp: Any = getattr(ctx, "stamp")
        stamp("time_left_minutes", "stats")
    return True
