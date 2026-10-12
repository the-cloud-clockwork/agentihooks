"""The tick's time left write: live slots and the CI median, with the calculation kept beside them."""

import ledger_stats

OPS = ("time_left",)
FIELDS = {"op", "id", "by", "slots", "ci_minutes"}
FOCUS = ("fix_phase", "watched")


def check(op):
    if not FIELDS <= set(op) <= FIELDS | set(FOCUS) or op["by"] != "swarm":
        raise ValueError("time_left takes id, by swarm, slots and ci_minutes, and optionally fix_phase and watched")
    if not isinstance(op.get("fix_phase", ""), str):
        raise ValueError("fix_phase must be text")
    watched = op.get("watched", [])
    if not isinstance(watched, list) or not all(isinstance(line, str) for line in watched):
        raise ValueError("watched must be a list of text")
    if op["slots"] is not None and (type(op["slots"]) is not int or op["slots"] < 0):
        raise ValueError("slots must be a nonnegative integer or null")
    ci = op["ci_minutes"]
    if ci is not None and (type(ci) not in (int, float) or ci < 0):
        raise ValueError("ci_minutes must be a nonnegative number or null")


def apply(doc, op, ctx):
    inputs = {"slots": op["slots"], "ci_minutes": op["ci_minutes"], **{key: op[key] for key in FOCUS if key in op}}
    result = ledger_stats.calculate(doc, ctx.meta["events"] + ctx.events, ctx.at, inputs)
    ctx.meta["time_left"] = {"at": ctx.at, "inputs": inputs, "calculation": result}
    ctx.dirty = True
    if result["minutes"] is not None and doc.get("time_left_minutes") != result["minutes"]:
        doc["time_left_minutes"] = result["minutes"]
        ctx.stamp("time_left_minutes", "stats")
    return True
