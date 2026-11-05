import json
import os
from collections.abc import Mapping
from typing import TYPE_CHECKING

from scripts.hive import registry
from scripts.routing.slots import account_key
from scripts.swarm_v2.runtime.base import LOCAL, RuntimeRouter

if TYPE_CHECKING:
    from redis import Redis


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    return RuntimeRouter.from_environ((), os.environ if environ is None else environ).spawn_backend() != LOCAL


def name(slot: str) -> str:
    """The row key of a ``harness:account`` slot, one key for its AH_CC_TOKEN_ slug and its Secret key spelling."""
    harness, account = slot.split(":", 1)
    return f"{harness}:{account_key(account)}"


def read(redis: "Redis", now: float) -> dict[str, dict]:
    from scripts.routing.reservation import LaunchSlots

    rows: dict[str, dict] = {}
    seen: set[str] = set()
    for hive in registry.hives(redis):
        if not registry.live(hive, int(now * 1000)):
            continue
        key = registry.key(hive["id"])
        raw = redis.get(f"{key}:sessions")
        if raw is None:
            continue
        counts = json.loads(raw)
        seen.update(execution for ids in counts.get("_executions", {}).values() for execution in ids)
        quotas = json.loads(redis.get(f"{key}:quota") or "{}")
        for slot in hive["slots"]:
            reported = f"{slot['harness']}:{slot['account']}"
            row = rows.setdefault(name(reported), {**slot, "sessions": 0, "quota": None})
            row["sessions"] += counts.get(reported, 0)
            row["masters"] = row.get("masters", 0) + counts.get(f"interactive:{reported}", 0)
            quota = quotas.get(reported)
            if quota is not None and quota["observed_at"] is not None:
                previous = row["quota"]
                if previous is None or quota["observed_at"] > previous["observed_at"]:
                    row.update(slot, quota=quota)
    for slot, count in LaunchSlots(redis).counts(seen).items():
        harness, account = slot.split(":", 1)
        row = rows.setdefault(
            name(slot),
            {"harness": harness, "account": account, "kind": "subscription", "quota": None, "sessions": 0},
        )
        row["sessions"] += count
    return rows


def current(now: float) -> dict[str, dict]:
    from hooks._redis import get_redis

    redis = get_redis()
    return {} if redis is None else read(redis, now)


def counts(harness: str, now: float) -> dict[str, int]:
    """Sessions per account under its AH_CC_TOKEN_ slug spelling, the name session and quota readers look up."""
    return {
        key.split(":", 1)[1].replace("-", "_"): row["sessions"]
        for key, row in current(now).items()
        if key.startswith(f"{harness}:")
    }


def elsewhere(redis: "Redis", harness: str, now: float, own: str) -> dict[str, int]:
    """Per account, what the launch reservation counts beyond this host's own hive report, which is its /proc."""
    from scripts.routing.reservation import LaunchSlots

    slots: dict[str, int] = {}
    seen: set[str] = set()
    for hive in registry.hives(redis):
        if not registry.live(hive, int(now * 1000)):
            continue
        report = json.loads(redis.get(f"{registry.key(hive['id'])}:sessions") or "{}")
        seen.update(execution for ids in report.get("_executions", {}).values() for execution in ids)
        if hive["id"] != own:
            for slot, count in report.items():
                if slot.startswith(f"{harness}:"):
                    slots[slot] = slots.get(slot, 0) + count
    for slot, count in LaunchSlots(redis).counts(seen).items():
        if slot.startswith(f"{harness}:"):
            slots[slot] = slots.get(slot, 0) + count
    return {slot.split(":", 1)[1]: count for slot, count in slots.items()}


def held_elsewhere(harness: str, now: float, own: str) -> dict[str, int]:
    from scripts.swarm.store import redis_client

    return elsewhere(redis_client(), harness, now, own)
