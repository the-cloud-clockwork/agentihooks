OPS = ("events_ack",)
FIELDS = {"op", "id", "by", "rev"}


def check(op):
    if set(op) != FIELDS or op["by"] != "swarm":
        raise ValueError("events_ack is the swarm's")
    if type(op["rev"]) is not int or op["rev"] < 0:
        raise ValueError("events_ack rev is invalid")


def apply(doc, op, ctx):
    if op["rev"] > ctx.meta.get("events_ack", -1):
        ctx.meta["events_ack"] = op["rev"]
        ctx.dirty = True
    return True
