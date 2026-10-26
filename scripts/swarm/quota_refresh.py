"""Answers quota refresh requests queued from the ledger page, probed on the hive as they land."""

import json
from collections.abc import Callable

from scripts.swarm import commands
from scripts.swarm.store import PREFIX, RedisStore, SwarmError

COMMAND = "quota"
WAKE = f"{PREFIX}:quota-refresh"
WAIT_S = 30
PAGE_TTL_S = 300


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


def run(store: RedisStore, execute: Callable[[str, dict], str], wait_s: float) -> None:
    pubsub = store.redis.pubsub()
    pubsub.subscribe(WAKE)
    while True:
        pubsub.get_message(timeout=wait_s)
        answer_all(store, execute)
