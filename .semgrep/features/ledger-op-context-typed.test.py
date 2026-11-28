from typing import Any

from scripts.swarm_ledger import ledger_core
from scripts.swarm_ledger.ledger_core import Context
from scripts.swarm_ledger.op_context import OpContext


# ruleid: feature-ledger-op-context-typed
def untyped(doc: dict[str, Any], op: dict[str, Any], ctx) -> bool:
    ctx.record(op["by"], "set", op["item"])
    return True


# ruleid: feature-ledger-op-context-typed
def by_any(doc: dict[str, Any], op: dict[str, Any], ctx: Any) -> bool:
    ctx.dirty = True
    return True


# ruleid: feature-ledger-op-context-typed
def by_object(op: dict[str, Any], ctx: object) -> str:
    return str(op["by"])


# ruleid: feature-ledger-op-context-typed
def by_the_concrete_class(doc: dict[str, Any], op: dict[str, Any], ctx: Context) -> bool:
    ctx.stamp(op["item"], op["by"])
    return True


class Gate:
    # ruleid: feature-ledger-op-context-typed
    def apply(self, doc: dict[str, Any], op: dict[str, Any], ctx, apply: Any) -> bool:
        return bool(apply(doc, op, ctx))


# ruleid: feature-ledger-op-context-typed
def ctx_first(ctx, by: str, rev: int) -> None:
    return None


# ruleid: feature-ledger-op-context-typed
def by_the_concrete_class_through_an_alias(doc: dict[str, Any], op: dict[str, Any], ctx: ledger_core.Context) -> bool:
    ctx.stamp(op["item"], op["by"])
    return True


# ok: feature-ledger-op-context-typed
def by_the_protocol(doc: dict[str, Any], op: dict[str, Any], ctx: OpContext) -> bool:
    ctx.record(op["by"], "set", op["item"])
    return True


# ok: feature-ledger-op-context-typed
def by_the_protocol_ctx_first(ctx: OpContext, by: str, rev: int) -> None:
    ctx.record(by, "set", "item")
    return None


# ok: feature-ledger-op-context-typed
def without_the_context(op: dict[str, Any]) -> str:
    return str(op["by"])
