import os
import sqlite3

from scripts.swarm import metrics_ledger, metrics_outbox
from scripts.swarm.ledger_client import LedgerClient

TICKS = metrics_outbox.Table("ticks", (("actions", "Int64"),))


def tick_row(slug, now_ms, actions):
    path = dict.fromkeys(("plan", "phase", "slice", "task"), "")
    return {"event_id": f"tick:{slug}:{now_ms}", "ledger": slug, "ts_ms": now_ms, **path, "actions": actions}


def record_pass(slug, now_ms, actions, environ=os.environ):
    sink = metrics_outbox.settings(environ)
    if sink is None:
        return []
    errors = []
    try:
        box = metrics_outbox.Outbox(metrics_outbox.spool_path(), sink)
        try:
            box.append(TICKS, [tick_row(slug, now_ms, actions)])
            try:
                metrics_ledger.record(box, slug, now_ms, LedgerClient())
            except OSError as exc:
                errors.append(f"ledger metrics failed: {exc}")
            box.flush(now_ms)
        finally:
            box.close()
    except (sqlite3.Error, OSError) as exc:
        return [f"metrics outbox failed: {exc}"]
    return errors
