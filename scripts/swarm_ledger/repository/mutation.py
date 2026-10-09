import copy

import ledger_alerts
import ledger_notifications
import ledger_plans
import ledger_priorities


def apply(slug, state, core, changes=None, ops=None, gate=None, created=False):
    """Fold checkbox changes and ops into the stored state in place; returns (rejected ids, the context)."""
    import ledger_artifacts
    import ledger_media

    doc, meta = state, state.pop("_meta")

    ctx = core.Context(meta, core.now_ms())
    meta.setdefault("members", {})
    meta["created_at"] = core.earliest(meta, ctx.at)
    rejected = core.apply_changes(doc, changes or [], ctx)
    ordered_ops = sorted(ops or [], key=lambda op: op["op"] == "stats_sync")
    unowned = len(ctx.refused)
    held = (copy.deepcopy(doc), dict(ctx.stamps), len(ctx.events)) if ledger_plans.atomic(ordered_ops) else None
    kept, raised = len(rejected), []
    for op in ordered_ops:
        start = len(ctx.refused)
        if core.gated(gate, doc, op, ctx):
            ledger_alerts.recovered(doc, op, ctx)
        else:
            rejected.append(op["id"])
        doc.setdefault("alerts", [])
        for text in ctx.refused[start:]:
            warning = (ledger_alerts.SYNC, text, ledger_alerts.writer(op, ctx), ledger_alerts.item(op))
            raised.append(warning)
            ledger_alerts.raise_warning(doc, ctx, warning, [])
    if held and len(rejected) > kept:
        rejected = rejected[:kept] + roll_back(doc, ctx, held, ordered_ops, raised)
    ledger_plans.drop_refused(doc, ordered_ops, rejected, ctx)
    ledger_artifacts.sweep(slug, doc, ctx)
    ledger_media.attach_paths(slug, doc, ctx.events)
    ledger_priorities.derive(doc, ctx)
    ledger_notifications.derive(doc, ctx)
    del doc["chat"][: -core.CHAT_KEPT]
    size = core.warnings(doc)
    found = size + ctx.refused
    ledger_alerts.derive(
        doc,
        ctx,
        [*((ledger_alerts.SIZE, w) for w in size), *((ledger_alerts.SYNC, w) for w in ctx.refused[:unowned])],
        meta.get("warnings") or [],
    )
    ctx.changed = bool(ctx.events or ctx.dirty or found != meta.get("warnings") or created)
    if ctx.changed:
        meta.update(rev=ctx.rev, updated_at=ctx.at, warnings=found)
        meta["events"] = (meta["events"] + ctx.events)[-core.EVENTS_KEPT :]
    state["_meta"] = meta
    return rejected, ctx


def roll_back(doc, ctx, held, ops, raised):
    before, stamps, events = held
    doc.clear()
    doc.update(before)
    ctx.stamps.clear()
    ctx.stamps.update(stamps)
    del ctx.events[events:]
    doc.setdefault("alerts", [])
    for warning in raised:
        ledger_alerts.raise_warning(doc, ctx, warning, [])
    return [op["id"] for op in ops]
