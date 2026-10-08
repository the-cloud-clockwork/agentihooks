import hashlib
import json
import os
import socket
import time
import uuid
from collections.abc import Callable

from scripts.hive import registry
from scripts.swarm import lease
from scripts.swarm.store import RedisStore


def hive_id() -> str:
    return os.environ.get("SWARM_HIVE_ID") or registry.seeded_id() or socket.gethostname()


def bind(store: RedisStore, slug: str, owner: str) -> bool:
    return lease.acquire(store, slug, owner) is not None


def submit(store: RedisStore, slug: str, command: str, argv: list[str], *, epoch: int | None = None) -> dict:
    store.config(slug)
    held = lease.current(store, slug)
    if epoch is not None and (held is None or held.epoch != epoch):
        raise lease.SwarmError("the controller lease is stale")
    payload = json.dumps([command, argv], separators=(",", ":"))
    row = {
        "id": uuid.uuid4().hex,
        "command": command,
        "argv": argv,
        "digest": hashlib.sha256(payload.encode()).hexdigest(),
        "owner": held.owner if held else "",
        "epoch": held.epoch if held else 0,
        "state": "pending",
        "created_at": time.time_ns() // 1_000_000,
    }
    with store.redis.pipeline() as pipe:
        pipe.hset(store.key(slug, "commands"), row["id"], json.dumps(row))
        pipe.rpush(store.key(slug, "command-pending"), row["id"])
        pipe.rpush(store.key(slug, "command-order"), row["id"])
        pipe.execute()
    return row


def rows(store: RedisStore, slug: str) -> list[dict]:
    found = store.redis.hgetall(store.key(slug, "commands"))
    order = store.redis.lrange(store.key(slug, "command-order"), 0, -1)
    return [
        row
        for index, key in enumerate(order)
        if (row := json.loads(found[key]))["state"] in {"pending", "accepted", "failed"} or index >= len(order) - 20
    ]


def consume(store: RedisStore, slug: str, owner: str, execute: Callable[[dict], str]) -> list[str]:
    held = lease.acquire(store, slug, owner)
    if held is None:
        return []
    pending, records = store.key(slug, "command-pending"), store.key(slug, "commands")
    actions = []
    for key in store.redis.lrange(pending, 0, -1):
        lease.require(store, slug, held)
        row = json.loads(store.redis.hgetall(records)[key])
        if row["state"] != "pending":
            store.redis.lrem(pending, 1, key)
            continue
        if row["owner"] and row["owner"] != owner:
            continue
        stale = row.get("epoch", 0) not in (0, held.epoch)
        row.update(owner=owner, epoch=held.epoch, state="accepted", accepted_at=time.time_ns() // 1_000_000)
        store.redis.hset(records, key, json.dumps(row))
        lease.require(store, slug, held)
        error = "the controller lease is stale" if stale else execute(row)
        row.update(
            state="failed" if error else "acknowledged", error=error, acknowledged_at=time.time_ns() // 1_000_000
        )
        lease.require(store, slug, held)
        with store.redis.pipeline() as pipe:
            pipe.hset(records, key, json.dumps(row))
            pipe.lrem(pending, 1, key)
            pipe.execute()
        actions.append(f"control {row['command']} {row['state']}")
    return actions


def publish(store: RedisStore, slug: str, status: dict, tails: dict) -> None:
    store.redis.set(store.key(slug, "published"), json.dumps({"status": status, "workspaces": tails}))


def view(store: RedisStore, slug: str) -> dict:
    config = store.config(slug)
    data = json.loads(store.redis.get(store.key(slug, "published")) or "{}")
    status = data.get("status", {"agents": [], "tasks": {}})
    return {
        **status,
        "config": {**status.get("config", config.__dict__), "state": config.state},
        "commands": rows(store, slug),
    }


def workspaces(store: RedisStore, slug: str) -> dict:
    data = json.loads(store.redis.get(store.key(slug, "published")) or "{}")
    return data.get("workspaces", {})
