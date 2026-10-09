from __future__ import annotations

import socket
import sys
from typing import TYPE_CHECKING
from uuid import uuid4

from scripts.inbox.store import InboxStore
from scripts.swarm import push
from scripts.swarm.store import PREFIX

if TYPE_CHECKING:
    from redis import Redis
    from redis.client import Pipeline

    from scripts.swarm.store import RedisStore


def key(kind: str) -> str:
    return f"{PREFIX}:host:{socket.gethostname()}:incident:{kind}"


def step(redis: Redis, kind: str, bad: bool) -> str:
    root = key(kind)

    def change(pipe: Pipeline) -> str:
        held = pipe.hgetall(root)
        active = held.get("active") == "1"
        field, other = ("bad", "good") if bad else ("good", "bad")
        count = int(held.get(field, 0)) + 1
        updates = {field: count, other: 0}
        event = ""
        if count >= 2 and bad != active:
            updates["active"] = int(bad)
            if bad:
                updates["generation"] = int(held.get("generation", 0)) + 1
            event = "raised" if bad else "resolved"
        pipe.multi()
        pipe.hset(root, mapping=updates)
        if event:
            pipe.rpush(f"{root}:outbox", event)
        return event

    return redis.transaction(change, root, value_from_callable=True)


def deliver(redis: Redis, kind: str, raised: str, resolved: str) -> None:
    root = key(kind)
    token = str(uuid4())
    lock_key = f"{root}:delivery"
    if not redis.set(lock_key, token, nx=True, px=30_000):
        return
    try:
        event = redis.lindex(f"{root}:outbox", 0)
        if event and push.send("critical" if kind == "ledger" else "alerts", raised if event == "raised" else resolved):
            redis.lpop(f"{root}:outbox")
    finally:

        def release(pipe: Pipeline) -> None:
            if pipe.get(lock_key) == token:
                pipe.multi()
                pipe.delete(lock_key)

        redis.transaction(release, lock_key)


def mail(redis: Redis, kind: str, address: str, text: str, resolved: bool = False) -> bool:
    root = key(kind)
    mail_key = f"{root}:mail:{address}"
    field = "resolved" if resolved else "raised"

    def claim(pipe: Pipeline) -> bool:
        generation, active = pipe.hmget(root, "generation", "active")
        generation = generation or "0"
        held = pipe.hgetall(mail_key)
        if not resolved and active != "1":
            return False
        if resolved and held.get("raised") != generation:
            return False
        if held.get(field) == generation:
            return False
        pipe.multi()
        pipe.hset(mail_key, field, generation)
        return True

    if not redis.transaction(claim, root, mail_key, value_from_callable=True):
        return False
    InboxStore(redis).send("swarm", address, text, fyi=resolved)
    return True


def host_pressure(store: RedisStore, slug: str) -> list[str]:
    from redis.exceptions import RedisError

    from scripts.swarm import host_budget, ledger_probe

    try:
        sample = host_budget.read_host()
        if sample is None:
            return []
        marks = host_budget.thresholds(store.config(slug))
        bad = sample.load1 / sample.cpus > marks.load_high or sample.available_mb < marks.memory_per_agent_mb
        step(store.redis, "pressure", bad)
        raised = "Host pressure: load is above the high mark or memory is below one agent share."
        resolved = "Host pressure resolved: load and available memory are within the host budget."
        deliver(store.redis, "pressure", raised, resolved)
        active = store.redis.hget(key("pressure"), "active") == "1"
        if mail(
            store.redis,
            "pressure",
            ledger_probe.master_address(store, slug),
            raised if active else resolved,
            not active,
        ):
            return ["raised the host pressure alert" if active else "cleared the host pressure alert"]
        return []
    except (OSError, RedisError):
        print("host pressure check unavailable", file=sys.stderr)
        return []
