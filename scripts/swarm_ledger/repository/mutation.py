import ledger_alerts
import ledger_notifications
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
    rejected += [op["id"] for op in ordered_ops if not core.gated(gate, doc, op, ctx)]
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
        [*((ledger_alerts.SIZE, w) for w in size), *((ledger_alerts.SYNC, w) for w in ctx.refused)],
        meta.get("warnings") or [],
    )
    ctx.changed = bool(ctx.events or ctx.dirty or found != meta.get("warnings") or created)
    if ctx.changed:
        meta.update(rev=ctx.rev, updated_at=ctx.at, warnings=found)
        meta["events"] = (meta["events"] + ctx.events)[-core.EVENTS_KEPT :]
    state["_meta"] = meta
    return rejected, ctx
