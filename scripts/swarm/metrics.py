import os
import sqlite3
from collections.abc import Mapping
from functools import partial

from scripts.swarm import (
    bottleneck,
    capacity,
    freeze,
    metrics_fleet,
    metrics_ledger,
    metrics_outbox,
    metrics_service,
    metrics_swarm,
    stage_detector,
    stages,
    timing,
)
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.store import SwarmError

TICKS = metrics_outbox.Table("ticks", (("actions", "Int64"),))


def tick_row(slug, now_ms, actions):
    path = dict.fromkeys(("plan", "phase", "slice", "task"), "")
    return {"event_id": f"tick:{slug}:{now_ms}", "ledger": slug, "ts_ms": now_ms, **path, "actions": actions}


def _collect(slug, now_ms, swarm, box, errors, environ):
    pulls = timing.call(metrics_swarm.pull_rows, box, now_ms, swarm)
    watched = freeze.watched(slug, swarm.store, swarm.ledger, swarm.doc)
    metrics_swarm.record_pass(box, slug, now_ms, swarm.store, watched, swarm.findings, pulls)
    bottleneck.record(box, swarm.store, slug, now_ms, swarm.doc.get("tasks", []))
    config = swarm.store.config(slug)
    seats = capacity.caps(capacity.read(swarm.store, slug), config)
    ready = stages.claimable(watched, freeze.fix_phase(config), seats)
    stage_detector.record(box, swarm.store, slug, now_ms, swarm.doc.get("tasks", []), environ, ready=ready)
    from redis.exceptions import RedisError

    from scripts.swarm_v2 import deployed

    try:
        metrics_fleet.record(box, swarm.store, slug, now_ms, pools=deployed.worker_pools(environ, slug))
    except (OSError, SwarmError, ValueError, KeyError, RedisError) as exc:
        errors.append(f"fleet metrics failed: {exc}")
    try:
        metrics_service.record(box, swarm.store, slug, now_ms)
    except (ValueError, KeyError, RedisError) as exc:
        errors.append(f"service metrics failed: {exc}")


def record(table, rows, now_ms, environ=os.environ, extra=None):
    sink = metrics_outbox.settings(environ)
    if sink is None:
        return []
    try:
        box = timing.call(metrics_outbox.Outbox, metrics_outbox.spool_path(), sink)
        try:
            box.append(table, rows)
            if extra is not None:
                extra(box)
            timing.call(box.flush, now_ms)
        finally:
            box.close()
    except (sqlite3.Error, OSError, ValueError) as exc:
        return [f"metrics outbox failed: {exc}"]
    return []


def _ledger(slug, now_ms, errors, box, swarm=None):
    try:
        if swarm is not None and swarm.ledger is not None:
            metrics_ledger.record(box, slug, now_ms, swarm.ledger, swarm.doc)
        else:
            metrics_ledger.record(box, slug, now_ms, LedgerClient())
    except (OSError, SwarmError) as exc:
        errors.append(f"ledger metrics failed: {exc}")


def _pass(slug, now_ms, swarm, errors, box, environ):
    timing.call(_ledger, slug, now_ms, errors, box, swarm)
    if swarm is not None:
        timing.call(_collect, slug, now_ms, swarm, box, errors, environ)


def record_pass(
    slug: str,
    now_ms: int,
    actions: int,
    environ: Mapping[str, str] = os.environ,
    swarm: metrics_swarm.TickInput | None = None,
) -> list[str]:
    if swarm is not None and swarm.store.config(slug).scratch:
        return []
    errors = []
    extra = partial(_pass, slug, now_ms, swarm, errors, environ=environ)
    return [*record(TICKS, [tick_row(slug, now_ms, actions)], now_ms, environ, extra), *errors]
