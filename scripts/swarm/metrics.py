import math
import os
import sqlite3
from collections.abc import Mapping
from functools import partial

from scripts.events import state
from scripts.events.bus import StateConflict
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
from scripts.swarm.store import SwarmError, store_errors

BUCKET = "metrics-interval"
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
    from scripts.swarm_v2 import deployed

    outage = store_errors()
    try:
        metrics_fleet.record(box, swarm.store, slug, now_ms, pools=deployed.worker_pools(environ, slug))
    except (OSError, SwarmError, ValueError, KeyError, *outage) as exc:
        errors.append(f"fleet metrics failed: {exc}")
    try:
        metrics_service.record(box, swarm.store, slug, now_ms)
    except (ValueError, KeyError, *outage) as exc:
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
    except (sqlite3.Error, OSError, ValueError, *state.unavailable(environ)) as exc:
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


def _interval_ms(environ):
    every_ms = float(environ.get("AGENTIHOOKS_METRICS_EVERY_MINUTES", "10")) * 60_000
    if not 1 <= every_ms < math.inf:
        raise ValueError("metrics collectors need a positive interval in minutes")
    return int(every_ms)


def _claim_interval(slug, store, now_ms, environ):
    every_ms = _interval_ms(environ)
    claims = state.for_store(store, BUCKET, environ)
    entry = claims.get(slug)
    if entry is not None and int(entry.value) + every_ms > now_ms:
        return False
    try:
        if entry is None:
            claims.create(slug, str(now_ms).encode())
        else:
            claims.update(slug, str(now_ms).encode(), entry.revision)
    except StateConflict:
        return False
    return True


def _reopen_interval(slug, store, environ):
    claims = state.for_store(store, BUCKET, environ)
    entry = claims.get(slug)
    if entry is not None:
        try:
            claims.update(slug, b"0", entry.revision)
        except StateConflict:
            return


def _pass(slug, now_ms, swarm, errors, box, environ):
    if swarm is None:
        timing.call(_ledger, slug, now_ms, errors, box)
        return
    if not _claim_interval(slug, swarm.store, now_ms, environ):
        return
    reported, failed = len(errors), True
    try:
        timing.call(_ledger, slug, now_ms, errors, box, swarm)
        timing.call(_collect, slug, now_ms, swarm, box, errors, environ)
        failed = len(errors) > reported
    finally:
        if failed:
            _reopen_interval(slug, swarm.store, environ)


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
