"""Order marks on phases and tasks."""

import re
from typing import Any

from scripts.swarm_ledger import ledger_relay

OPS = ("order_set", "order_clear")
MARK = "ordered"
NOUN = "order"
ITEM_RE = re.compile(r"(phases|tasks)/[^/\s]+")
PROOFS = ("quote", "request")
FIELDS = {"order_set": ("item", *PROOFS, "filing"), "order_clear": ("item", "quote")}
EVENTS = {"order_set": "ordered", "order_clear": "order cleared"}
NEEDS = {
    "order_set": "an agent's order needs quote, the operator's words, or request, the id of an entry he wrote",
    "order_clear": "an agent clears an order only with quote, the operator's words",
}


def check(op: dict[str, Any]) -> None:
    kind: str = op["op"]
    if set(op) - {"op", "id", "by", *FIELDS[kind]}:
        raise ValueError(f"{kind} takes only {' '.join(FIELDS[kind])}")
    by = op.get("by")
    if by is not None and (not isinstance(by, str) or not ledger_relay.AUTHOR_RE.match(by) or by == "operator"):
        raise ValueError("order ops take by, the relaying agent's name, or none for the operator")
    if not ITEM_RE.fullmatch(str(op.get("item"))):
        raise ValueError("an order names phases/<id> or tasks/<id>")
    if "filing" in op and op["filing"] is not True:
        raise ValueError("filing is true on an order sent with the add of its item")
    given = [key for key in PROOFS if key in op]
    for key in given:
        text = op[key]
        if not isinstance(text, str) or not text.strip() or len(text) > ledger_relay.MAX_TEXT:
            raise ValueError(f"{key} must be text up to {ledger_relay.MAX_TEXT} characters")
    if len(given) > 1:
        raise ValueError("an order takes quote or request, not both")
    if by is not None and not given:
        raise ValueError(NEEDS[kind])


def _refuse(op: dict[str, Any], ctx: Any, reason: str) -> bool:
    ctx.refused.append(f"order on {op['item']} refused: {reason}")
    return False


def _proof(doc: dict[str, Any], op: dict[str, Any], ctx: Any) -> dict[str, str] | None:
    """The proof an order carries."""
    if "by" not in op:
        return {key: op[key] for key in PROOFS if key in op}
    if "quote" in op:
        words = ledger_relay.orchestrator_words(op, ctx, NOUN)
        return {"quote": words} if words else None
    if not ledger_relay.orchestrator(op, ctx, NOUN):
        return None
    from scripts.swarm_ledger import ledger_artifacts

    if ledger_artifacts.operator_asked(doc, op["request"]):
        return {"request": op["request"]}
    _refuse(op, ctx, f"the entry {op['request']} is not one the operator wrote")
    return None


def apply(doc: dict[str, Any], op: dict[str, Any], ctx: Any) -> bool:
    name, item_id = op["item"].split("/")
    rows: list[dict[str, Any]] = doc[name]
    item = next((row for row in rows if row["id"] == item_id), None)
    if item is None:
        return _refuse(op, ctx, "the entry does not exist")
    if "filing" in op and not any(e["kind"] == "added" and e["target"] == op["item"] for e in ctx.events):
        return _refuse(op, ctx, "this call did not add the entry")
    proof = _proof(doc, op, ctx)
    if proof is None:
        return False
    setting = op["op"] == "order_set"
    if (MARK in item) == setting:
        return True
    marks: dict[str, str] = {}
    if "by" in op:
        marks = ledger_relay.marks_of(op, proof)
    if setting:
        item[MARK] = {"at": ctx.at, "by": op.get("by", "operator"), **proof}
    else:
        del item[MARK]
    ctx.record("operator", EVENTS[op["op"]], op["item"], id=op["id"], **marks)
    ctx.stamp(f"{op['item']}/{MARK}", "operator")
    return True
