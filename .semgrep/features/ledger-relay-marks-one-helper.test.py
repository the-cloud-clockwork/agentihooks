from scripts.swarm_ledger import ledger_relay
from scripts.swarm_ledger.ledger_relay import RELAYED_FROM


def by_hand(op, ctx, words):
    # ruleid: feature-ledger-relay-marks-one-helper
    marks = {"relayed_by": op["by"], "relayed_from": RELAYED_FROM, "quote": words}
    ctx.record("operator", "rank set", op["item"], **marks)


def the_other_way_round(op, request):
    # ruleid: feature-ledger-relay-marks-one-helper
    return {"relayed_from": ledger_relay.RELAYED_FROM, "relayed_by": op["by"], "request": request}


def with_the_proof_spread(op, proof):
    # ruleid: feature-ledger-relay-marks-one-helper
    return {"relayed_by": op["by"], "relayed_from": ledger_relay.RELAYED_FROM, **proof}


def with_the_proof_spread_first(op, proof):
    # ruleid: feature-ledger-relay-marks-one-helper
    return {**proof, "relayed_from": ledger_relay.RELAYED_FROM, "relayed_by": op["by"]}


def as_keywords(op, ctx, words):
    # ruleid: feature-ledger-relay-marks-one-helper
    ctx.record("operator", "ordered", op["item"], relayed_by=op["by"], relayed_from=RELAYED_FROM, quote=words)


def through_the_helper(op, ctx, words):
    # ok: feature-ledger-relay-marks-one-helper
    marks = ledger_relay.marks_of(op, {"quote": words})
    ctx.record("operator", "rank set", op["item"], **marks)


def reads_one_mark(entry):
    # ok: feature-ledger-relay-marks-one-helper
    return {"relayed_by": entry.get("relayed_by")}
