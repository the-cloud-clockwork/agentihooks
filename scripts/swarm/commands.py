import hashlib
import json
import os
import socket
import time
import uuid
from collections.abc import Callable

from scripts.hive import registry
from scripts.swarm import bottleneck_status, execution_card, lease
from scripts.swarm.store import RedisStore
from scripts.swarm_v2.herdr import visibility


def hive_id() -> str:
    from scripts.hive import scope

    return scope.member() or os.environ.get("SWARM_HIVE_ID") or registry.seeded_id() or socket.gethostname()


def bind(store: RedisStore, slug: str, owner: str) -> bool:
    return lease.acquire(store, slug, owner) is not None


def prepare(store: RedisStore, slug: str, command: str, argv: list[str], *, epoch: int | None = None) -> dict:
    store.config(slug)
    held = lease.current(store, slug)
    if epoch is not None and (held is None or held.epoch != epoch):
        raise lease.SwarmError("the controller lease is stale")
    payload = json.dumps([command, argv], separators=(",", ":"))
    return {
        "id": uuid.uuid4().hex,
        "command": command,
        "argv": argv,
        "digest": hashlib.sha256(payload.encode()).hexdigest(),
        "owner": held.owner if held else "",
        "epoch": held.epoch if held else 0,
        "state": "pending",
        "created_at": time.time_ns() // 1_000_000,
    }


def enqueue(pipe, store: RedisStore, slug: str, row: dict) -> None:
    pipe.hset(store.key(slug, "commands"), row["id"], json.dumps(row))
    pipe.rpush(store.key(slug, "command-pending"), row["id"])
    pipe.rpush(store.key(slug, "command-order"), row["id"])


def submit(store: RedisStore, slug: str, command: str, argv: list[str], *, epoch: int | None = None) -> dict:
    row = prepare(store, slug, command, argv, epoch=epoch)
    with store.redis.pipeline() as pipe:
        enqueue(pipe, store, slug, row)
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


def claim(store: RedisStore, records: str, key: str, owner: str, epoch: int) -> dict | None:
    """Accept a pending row once, though the tick and the quota prober read the same queue."""
    from redis.exceptions import WatchError

    with store.redis.pipeline() as pipe:
        try:
            pipe.watch(records)
            row = json.loads(pipe.hget(records, key))
            if row["state"] != "pending":
                return None
            row.update(owner=owner, epoch=epoch, state="accepted", accepted_at=time.time_ns() // 1_000_000)
            pipe.multi()
            pipe.hset(records, key, json.dumps(row))
            pipe.execute()
        except WatchError:
            return None
    return row


def consume(store: RedisStore, slug: str, owner: str, execute: Callable[[dict], str], only: str = "") -> list[str]:
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
        if (row["owner"] and row["owner"] != owner) or (only and row["command"] != only):
            continue
        stale = row.get("epoch", 0) not in (0, held.epoch)
        row = claim(store, records, key, owner, held.epoch)
        if row is None:
            continue
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
    from scripts.swarm import lifecycle, quota_refresh, recovery_view, tick_activity

    config = store.config(slug)
    data = json.loads(store.redis.get(store.key(slug, "published")) or "{}")
    status = data.get("status", {"agents": [], "tasks": {}})
    quota = quota_refresh.latest(store, slug, status.get("quota"))
    status = status if quota is None else {**status, "quota": quota}
    now = time.time_ns() // 1_000_000
    terminals = visibility.read(store, slug, now)
    executions, errors = execution_card.cards(status.get("agents", []), terminals)
    return {
        **status,
        "last_tick_activity": tick_activity.read(store, slug),
        "top_bottleneck": bottleneck_status.report(store, slug, now),
        "config": {**status.get("config", config.__dict__), "state": config.state, "autonomy": config.autonomy},
        "commands": rows(store, slug),
        "terminals": terminals,
        "executions": executions,
        "ledger_state_projection_errors": errors,
        "lifecycle": lifecycle.view(store, slug),
        "recovery": recovery_view.view(store, slug, now),
    }


def workspaces(store: RedisStore, slug: str) -> dict:
    data = json.loads(store.redis.get(store.key(slug, "published")) or "{}")
    return data.get("workspaces", {})
