import copy
from typing import Any

import ledger_alerts
import ledger_notifications
import ledger_plans
import ledger_priorities

from .events import keep_events


def apply(
    slug: str,
    state: dict[str, Any],
    core: Any,
    changes: list[Any] | None = None,
    ops: list[dict[str, Any]] | None = None,
    gate: Any = None,
    created: bool = False,
) -> tuple[list[str], Any]:
    """Fold checkbox changes and ops into the stored state in place; returns (rejected ids, the context)."""
    import ledger_artifacts
    import ledger_media

    doc, meta = state, state.pop("_meta")

    ctx = core.Context(meta, core.now_ms(), slug)
    meta.setdefault("members", {})
    meta["created_at"] = core.earliest(meta, ctx.at)
    rejected = core.apply_changes(doc, changes or [], ctx)
    ordered_ops = sorted(ops or [], key=lambda op: op["op"] == "stats_sync")
    unowned = len(ctx.refused)
    held = snapshot(doc, ctx) if ledger_plans.atomic(ordered_ops) else None
    kept = len(rejected)
    raised = run(doc, ctx, core, gate, ordered_ops, rejected)
    if held and len(rejected) > kept:
        rejected = rejected[:kept] + roll_back(doc, ctx, held, ordered_ops, raised)
    ledger_plans.drop_refused(doc, ordered_ops, rejected, ctx)
    ledger_artifacts.sweep(slug, doc, ctx)
    ledger_media.attach_paths(slug, doc, ctx.events)
    ledger_priorities.derive(doc, ctx)
    ledger_notifications.derive(doc, ctx)
    del doc["chat"][: -core.CHAT_KEPT]
    size = core.warnings(doc)
    finish(doc, ctx, core, meta, size, ctx.refused[:unowned], created)
    state["_meta"] = meta
    return rejected, ctx


def run(
    doc: dict[str, Any], ctx: Any, core: Any, gate: Any, ops: list[dict[str, Any]], rejected: list[str]
) -> list[tuple[Any, ...]]:
    """Apply each op through the gate, closing the alerts a success recovers and raising one per refusal; returns the
    raised warnings and appends each rejected op id to rejected."""
    raised: list[tuple[Any, ...]] = []
    for op in ops:
        start = len(ctx.refused)
        if core.gated(gate, doc, op, ctx):
            ledger_alerts.recovered(doc, op, ctx)
        else:
            rejected.append(op["id"])
        doc.setdefault("alerts", [])
        alerting: list[str] = [text for text in ctx.refused[start:] if not ledger_alerts.settling_refusal(text)]
        for text in alerting:
            warning = (ledger_alerts.SYNC, text, ledger_alerts.writer(op, ctx), ledger_alerts.item(op))
            raised.append(warning)
            ledger_alerts.raise_warning(doc, ctx, warning, [])
    return raised


def finish(
    doc: dict[str, Any], ctx: Any, core: Any, meta: dict[str, Any], size: list[str], unowned: list[str], created: bool
) -> None:
    """Derive the alerts, then stamp the revision, time, warnings and kept events when anything changed."""
    found = size + ctx.refused
    ledger_alerts.derive(
        doc,
        ctx,
        [*((ledger_alerts.SIZE, w) for w in size), *((ledger_alerts.SYNC, w) for w in unowned)],
        meta.get("warnings") or [],
    )
    ctx.changed = bool(ctx.events or ctx.dirty or found != meta.get("warnings") or created)
    if ctx.changed:
        meta.update(rev=ctx.rev, updated_at=ctx.at, warnings=found)
        keep_events(meta, ctx.events, core, ctx.at)


def narrow(slug: str, state: dict[str, Any], core: Any, op: dict[str, Any], gate: Any) -> tuple[list[str], Any]:
    """Fold one claim shaped task update into a partial state holding its task, the rows it reads, every alert and the
    meta keys the update touches; the passes a claim cannot change are skipped, so no other part is needed."""
    doc, meta = state, state.pop("_meta")
    ctx = core.Context(meta, core.now_ms(), slug)
    rejected: list[str] = []
    run(doc, ctx, core, gate, [op], rejected)
    stored: list[str] = meta.get("warnings") or []
    finish(doc, ctx, core, meta, [warning for warning in stored if core.size_warning(warning)], [], False)
    state["_meta"] = meta
    return rejected, ctx


def snapshot(doc, ctx):
    return copy.deepcopy(doc), dict(ctx.stamps), len(ctx.events), ctx.dirty, len(ctx.dropped)


def roll_back(doc, ctx, held, ops, raised):
    before, stamps, events, dirty, dropped = held
    doc.clear()
    doc.update(before)
    ctx.stamps.clear()
    ctx.stamps.update(stamps)
    del ctx.events[events:]
    del ctx.dropped[dropped:]
    ctx.dirty = dirty
    doc.setdefault("alerts", [])
    for warning in raised:
        ledger_alerts.raise_warning(doc, ctx, warning, [])
    return [op["id"] for op in ops]
