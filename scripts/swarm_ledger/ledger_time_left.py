"""The tick's time left write: live slots and the CI median, with the calculation kept beside them."""

import ledger_stats

OPS = ("time_left",)
FIELDS = {"op", "id", "by", "slots", "ci_minutes"}


def check(op):
    if set(op) != FIELDS or op["by"] != "swarm":
        raise ValueError("time_left takes id, by swarm, slots and ci_minutes")
    if op["slots"] is not None and (type(op["slots"]) is not int or op["slots"] < 0):
        raise ValueError("slots must be a nonnegative integer or null")
    ci = op["ci_minutes"]
    if ci is not None and (type(ci) not in (int, float) or ci < 0):
        raise ValueError("ci_minutes must be a nonnegative number or null")


def apply(doc, op, ctx):
    inputs = {"slots": op["slots"], "ci_minutes": op["ci_minutes"]}
    result = ledger_stats.calculate(doc, ctx.meta["events"] + ctx.events, ctx.at, inputs)
    if ctx.meta.get("time_left", {}).get("calculation") != result:
        ctx.meta["time_left"] = {"at": ctx.at, "inputs": inputs, "calculation": result}
        ctx.dirty = True
    if result["minutes"] is not None and doc.get("time_left_minutes") != result["minutes"]:
        doc["time_left_minutes"] = result["minutes"]
        ctx.stamp("time_left_minutes", "stats")
    return True
