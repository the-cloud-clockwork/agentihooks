import os
import sqlite3
from functools import partial

from scripts.swarm import bottleneck, metrics_outbox, metrics_swarm

TICKS = metrics_outbox.Table("ticks", (("actions", "Int64"),))


def tick_row(slug, now_ms, actions):
    path = dict.fromkeys(("plan", "phase", "slice", "task"), "")
    return {"event_id": f"tick:{slug}:{now_ms}", "ledger": slug, "ts_ms": now_ms, **path, "actions": actions}


def _collect(slug, now_ms, swarm, box):
    pulls = metrics_swarm.pull_rows(box, now_ms, swarm)
    metrics_swarm.record_pass(box, slug, now_ms, swarm.store, swarm.doc, swarm.findings, pulls)
    bottleneck.record(box, swarm.store, slug, now_ms, swarm.doc.get("tasks", []))


def record(table, rows, now_ms, environ=os.environ, extra=None):
    sink = metrics_outbox.settings(environ)
    if sink is None:
        return []
    try:
        box = metrics_outbox.Outbox(metrics_outbox.spool_path(), sink)
        try:
            box.append(table, rows)
            if extra is not None:
                extra(box)
            box.flush(now_ms)
        finally:
            box.close()
    except (sqlite3.Error, OSError, ValueError) as exc:
        return [f"metrics outbox failed: {exc}"]
    return []


def record_pass(slug, now_ms, actions, environ=os.environ, swarm=None):
    extra = None if swarm is None else partial(_collect, slug, now_ms, swarm)
    return record(TICKS, [tick_row(slug, now_ms, actions)], now_ms, environ, extra)
