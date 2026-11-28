"""The tick's time left write: live slots and the CI median, with the calculation kept beside them."""

import ledger_stats

from scripts.swarm_ledger.op_context import OpContext

OPS = ("time_left",)
FIELDS = {"op", "id", "by", "slots", "ci_minutes"}
FOCUS = ("fix_phase", "watched")
OPTIONAL = (*FOCUS, "lab")


def check(op: dict) -> None:
    if not FIELDS <= set(op) <= FIELDS | set(OPTIONAL) or op["by"] != "swarm":
        raise ValueError(
            "time_left takes id, by swarm, slots and ci_minutes, and optionally fix_phase, watched and lab"
        )
    if not isinstance(op.get("fix_phase", ""), str):
        raise ValueError("fix_phase must be text")
    if not isinstance(op.get("lab", {}), dict):
        raise ValueError("lab must be an object")
    watched = op.get("watched", [])
    if not isinstance(watched, list) or not all(isinstance(line, str) for line in watched):
        raise ValueError("watched must be a list of text")
    if op["slots"] is not None and (type(op["slots"]) is not int or op["slots"] < 0):
        raise ValueError("slots must be a nonnegative integer or null")
    ci = op["ci_minutes"]
    if ci is not None and (type(ci) not in (int, float) or ci < 0):
        raise ValueError("ci_minutes must be a nonnegative number or null")


def apply(doc: dict, op: dict, ctx: OpContext) -> bool:
    inputs = {"slots": op["slots"], "ci_minutes": op["ci_minutes"], **{key: op[key] for key in OPTIONAL if key in op}}
    result = ledger_stats.calculate(doc, ctx.meta["events"] + ctx.events, ctx.at, inputs)
    ctx.meta["time_left"] = {"at": ctx.at, "inputs": inputs, "calculation": result}
    ctx.dirty = True
    if result["minutes"] is not None and doc.get("time_left_minutes") != result["minutes"]:
        doc["time_left_minutes"] = result["minutes"]
        ctx.stamp("time_left_minutes", "stats")
    return True
