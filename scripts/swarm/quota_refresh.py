"""Answers quota refresh requests queued from the ledger page, probed on the hive as they land."""

import json
import time
from collections.abc import Callable

from scripts.swarm import commands
from scripts.swarm.store import PREFIX, RedisStore, SwarmError

COMMAND = "quota"
WAKE = f"{PREFIX}:quota-refresh"
WAIT_S = 30
PAGE_TTL_S = 300
PROBE_S = 60


def request(store: RedisStore, slug: str) -> dict:
    row = commands.submit(store, slug, COMMAND, [])
    store.redis.publish(WAKE, slug)
    return row


def page_key(store: RedisStore, slug: str) -> str:
    return store.key(slug, "quota-page")


def answer(store: RedisStore, slug: str, execute: Callable[[str, dict], str]) -> list[str]:
    from scripts import agents_quota

    if not any(row["command"] == COMMAND and row["state"] == "pending" for row in commands.rows(store, slug)):
        return []
    actions = commands.consume(store, slug, commands.hive_id(), lambda row: execute(slug, row), only=COMMAND)
    if f"control {COMMAND} acknowledged" in actions:
        store.redis.set(page_key(store, slug), json.dumps(agents_quota.page_quota()), ex=PAGE_TTL_S)
    return actions


def fresher(published: dict | None, probed: dict | None) -> dict | None:
    if probed is None:
        return published
    if published is None or (probed.get("probed_at") or 0) >= (published.get("probed_at") or 0):
        return probed
    return published


def latest(store: RedisStore, slug: str, published: dict | None) -> dict | None:
    raw = store.redis.get(page_key(store, slug))
    return fresher(published, json.loads(raw) if raw else None)


def answer_all(store: RedisStore, execute: Callable[[str, dict], str]) -> list[str]:
    lines = []
    for slug in store.slugs():
        try:
            lines += [f"{slug}: {action}" for action in answer(store, slug, execute)]
        except SwarmError as exc:
            lines.append(f"{slug}: {exc}")
    for line in lines:
        print(line, flush=True)
    return lines


def probe(environ: dict[str, str], now: float) -> list[str]:
    """Probe every stale quota reading so the tick pass reads fresh caches, logging one line per probe."""
    from scripts.swarm import capacity

    line = f"quota probe read {len(capacity.probe(environ, now))} accounts"
    print(line, flush=True)
    return [line]


def probe_due(prober: Callable[[], object], due: float, now: float) -> float:
    """Run ``prober`` once ``due`` has passed and return when the next probe is due."""
    if now < due:
        return due
    prober()
    return now + PROBE_S


def run(store: RedisStore, execute: Callable[[str, dict], str], wait_s: float, prober: Callable[[], object]) -> None:
    pubsub = store.redis.pubsub()
    pubsub.subscribe(WAKE)
    due = 0.0
    while True:
        due = probe_due(prober, due, time.monotonic())
        pubsub.get_message(timeout=min(wait_s, max(0.0, due - time.monotonic())))
        answer_all(store, execute)
