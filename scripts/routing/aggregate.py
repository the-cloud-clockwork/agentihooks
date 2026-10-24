import json
import os
from collections.abc import Mapping
from typing import TYPE_CHECKING

from scripts.hive import registry
from scripts.swarm_v2.runtime.base import LOCAL, RuntimeRouter

if TYPE_CHECKING:
    from redis import Redis


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    return RuntimeRouter.from_environ((), os.environ if environ is None else environ).spawn_backend() != LOCAL


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
            name = f"{slot['harness']}:{slot['account']}"
            row = rows.setdefault(name, {**slot, "sessions": 0, "quota": None})
            row["sessions"] += counts.get(name, 0)
            row["masters"] = row.get("masters", 0) + counts.get(f"interactive:{name}", 0)
            quota = quotas.get(name)
            if quota is not None and quota["observed_at"] is not None:
                previous = row["quota"]
                if previous is None or quota["observed_at"] > previous["observed_at"]:
                    row.update(slot, quota=quota)
    for name, count in LaunchSlots(redis).counts(seen).items():
        harness, account = name.split(":", 1)
        row = rows.setdefault(
            name, {"harness": harness, "account": account, "kind": "subscription", "quota": None, "sessions": 0}
        )
        row["sessions"] += count
    return rows


def current(now: float) -> dict[str, dict]:
    from hooks._redis import get_redis

    redis = get_redis()
    return {} if redis is None else read(redis, now)


def counts(harness: str, now: float) -> dict[str, int]:
    return {row["account"]: row["sessions"] for row in current(now).values() if row["harness"] == harness}


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
            for name, count in report.items():
                if name.startswith(f"{harness}:"):
                    slots[name] = slots.get(name, 0) + count
    for name, count in LaunchSlots(redis).counts(seen).items():
        if name.startswith(f"{harness}:"):
            slots[name] = slots.get(name, 0) + count
    return {name.split(":", 1)[1]: count for name, count in slots.items()}
