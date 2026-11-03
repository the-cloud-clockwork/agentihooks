"""Hive launch acknowledgements collected by the fifteen second wait pass, apart from the long tick pass.

The tick's collect acks step and this pass share one per swarm lock, so one collector records a launch at a time.
"""

import json
import os
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from scripts.swarm.store import RedisStore

LOCK = "acks-lock"
LOCK_MS = 30_000
RENEW_S = 10.0
COLLECTING = ("acknowledging", "launching")


@contextmanager
def collecting(store: RedisStore, slug: str) -> Iterator[bool]:
    """Hold the swarm's acks lock for the block, renewed while held, yielding whether this caller took it."""
    from scripts.scheduler.process_interval import Interval

    key, token = store.key(slug, LOCK), uuid.uuid4().hex
    if not store.redis.set(key, token, nx=True, px=LOCK_MS):
        yield False
        return
    renewing = Interval(lambda: _renew(store, key, token), every=RENEW_S)
    renewing.start()
    try:
        yield True
    finally:
        try:
            if store.redis.get(key) == token:
                store.redis.delete(key)
        finally:
            renewing.close()


def _renew(store: RedisStore, key: str, token: str) -> None:
    """Extend the acks lock while this holder still owns it; a Redis error is reported and the next beat retries."""
    from redis.exceptions import RedisError

    try:
        if store.redis.get(key) == token:
            store.redis.pexpire(key, LOCK_MS)
    except RedisError as exc:
        print(f"renewing the acks lock {key} failed: {type(exc).__name__}", file=sys.stderr)


def due(store: RedisStore, slug: str) -> bool:
    """Whether any launch waits for its acknowledgement or its workstation launch job."""
    from scripts.swarm import launch_job

    if store.redis.hlen(store.key(slug, launch_job.KEY)):
        return True
    rows = store.redis.hvals(store.key(slug, "launches"))
    return any(json.loads(raw).get("state") in COLLECTING for raw in rows)


def run(store: RedisStore, slug: str, ledger: Any = None, runtime: Any = None) -> list[str]:
    """Collect the swarm's launch acknowledgements when this hive holds its lease and no other collector runs."""
    from scripts.swarm_ledger import authority_context, ledger_link, ledger_route

    with ledger_route.scoped(slug, ledger_route.configured(store, slug)):
        if ledger_link.remote(slug=slug) and not authority_context.tick_headers(slug):
            return []
        return _collect(store, slug, ledger, runtime)


def _collect(store: RedisStore, slug: str, ledger: Any, runtime: Any) -> list[str]:
    from scripts.swarm import commands, controller, lease, ledger_client, timing
    from scripts.swarm import tick as engine

    held = lease.acquire(store, slug, commands.hive_id())
    if held is None or not due(store, slug):
        return []
    config = store.config(slug)
    if config.state == "stopped":
        return []
    with collecting(store, slug) as taken:
        if not taken:
            return []
        ledger = ledger or ledger_client.LedgerClient()
        doc = ledger_client.pass_state(ledger, slug)
        fenced = controller.FencedRuntime(
            store, slug, held, runtime or _runtime(), controller.deployment_for(config, os.environ)
        )
        lease.require(store, slug, held)
        with timing.tick(slug):
            return timing.call(engine._collect_acks, slug, config, store, ledger, fenced, doc, int(time.time() * 1000))


def _runtime() -> Any:
    from scripts.swarm.runtime import HerdrRuntime, deferring
    from scripts.swarm_v2.runtime.routed import routed

    return routed(herdr=deferring(HerdrRuntime()))
