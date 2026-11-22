"""Order marks on phases and tasks."""

import re
from typing import Any

from scripts.swarm_ledger import ledger_relay

OPS = ("order_set", "order_clear")
MARK = "ordered"
NOUN = "order"
ITEM_RE = re.compile(r"(phases|tasks)/[^/\s]+")
PROOFS = ("quote", "request")
FIELDS = {"order_set": ("item", *PROOFS), "order_clear": ("item", "quote")}
EVENTS = {"order_set": "ordered", "order_clear": "order cleared"}


def check(op: dict[str, Any]) -> None:
    kind: str = op["op"]
    if set(op) - {"op", "id", "by", *FIELDS[kind]}:
        raise ValueError(f"{kind} takes only {' '.join(FIELDS[kind])}")
    by = op.get("by")
    if by is not None and (not isinstance(by, str) or not ledger_relay.AUTHOR_RE.match(by) or by == "operator"):
        raise ValueError("order ops take by, the relaying agent's name, or none for the operator")
    if not ITEM_RE.fullmatch(str(op.get("item"))):
        raise ValueError("an order names phases/<id> or tasks/<id>")
    given = [key for key in PROOFS if key in op]
    for key in given:
        text = op[key]
        if not isinstance(text, str) or not text.strip() or len(text) > ledger_relay.MAX_TEXT:
            raise ValueError(f"{key} must be text up to {ledger_relay.MAX_TEXT} characters")
    if by is not None and kind == "order_clear" and not given:
        raise ValueError("an agent clears an order only with quote, the operator's words")
    if by is not None and kind == "order_set" and len(given) != 1:
        raise ValueError("an agent's order needs quote, the operator's words, or request, the id of an entry he wrote")


def _asked(doc: dict[str, Any], entry: str) -> bool:
    """Whether the operator wrote the ledger entry."""
    from scripts.swarm_ledger import ledger_artifacts

    artifacts: Any = ledger_artifacts
    found: bool = artifacts.operator_asked(doc, entry)
    return found


def _proof(doc: dict[str, Any], op: dict[str, Any], ctx: Any) -> dict[str, str] | None:
    """The proof an order carries."""
    if "by" not in op:
        return {key: op[key] for key in PROOFS if key in op}
    if "quote" in op:
        words = ledger_relay.orchestrator_words(op, ctx, NOUN)
        return {"quote": words} if words else None
    if not ledger_relay.orchestrator(op, ctx, NOUN):
        return None
    if not _asked(doc, op["request"]):
        ctx.refused.append(f"order on {op['item']} refused: the entry {op['request']} is not one the operator wrote")
        return None
    return {"request": op["request"]}


def apply(doc: dict[str, Any], op: dict[str, Any], ctx: Any) -> bool:
    name, item_id = op["item"].split("/")
    rows: list[dict[str, Any]] = doc[name]
    item = next((row for row in rows if row["id"] == item_id), None)
    if item is None:
        ctx.refused.append(f"order on {op['item']} refused: the entry does not exist")
        return False
    proof = _proof(doc, op, ctx)
    if proof is None:
        return False
    setting = op["op"] == "order_set"
    if (MARK in item) == setting:
        return True
    marks: dict[str, str] = {}
    if "by" in op:
        marks = {"relayed_by": op["by"], "relayed_from": ledger_relay.RELAYED_FROM, **proof}
    if setting:
        item[MARK] = {"at": ctx.at, "by": op.get("by", "operator"), **proof}
    else:
        del item[MARK]
    ctx.record("operator", EVENTS[op["op"]], op["item"], id=op["id"], **marks)
    ctx.stamp(f"{op['item']}/{MARK}", "operator")
    return True
