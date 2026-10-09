import os
import sqlite3

from scripts.swarm import bottleneck, metrics_outbox, metrics_swarm

TICKS = metrics_outbox.Table("ticks", (("actions", "Int64"),))


def tick_row(slug, now_ms, actions):
    path = dict.fromkeys(("plan", "phase", "slice", "task"), "")
    return {"event_id": f"tick:{slug}:{now_ms}", "ledger": slug, "ts_ms": now_ms, **path, "actions": actions}


def record_pass(slug, now_ms, actions, environ=os.environ, swarm=None):
    sink = metrics_outbox.settings(environ)
    if sink is None:
        return []
    try:
        box = metrics_outbox.Outbox(metrics_outbox.spool_path(), sink)
        try:
            box.append(TICKS, [tick_row(slug, now_ms, actions)])
            if swarm is not None:
                pulls = metrics_swarm.pull_rows(box, now_ms, swarm)
                metrics_swarm.record_pass(box, slug, now_ms, swarm.store, swarm.doc, swarm.findings, pulls)
                bottleneck.record(box, swarm.store, slug, now_ms, swarm.doc.get("tasks", []))
            box.flush(now_ms)
        finally:
            box.close()
    except (sqlite3.Error, OSError, ValueError) as exc:
        return [f"metrics outbox failed: {exc}"]
    return []
