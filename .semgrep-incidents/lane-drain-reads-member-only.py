from typing import Any

from scripts.hive import registry


def draining(store: Any, hive: str) -> bool:
    redis = getattr(store, "redis", None)
    # ruleid: lane-drain-reads-member-only
    return redis is not None and registry.member_draining(redis, hive, registry.member_name())


def drained(store: Any, hive: str) -> bool:
    redis = getattr(store, "redis", None)
    if redis is None:
        return False
    # ok: lane-drain-reads-member-only
    record: dict[str, Any] = registry.show(redis, hive) or {}
    member = record.get("members", {}).get(registry.member_name(), {})
    return "yes" in (record.get("draining"), member.get("draining"))


def member_stopped(redis: Any, hive: str) -> bool:
    # ok: lane-drain-reads-member-only
    return registry.member_draining(redis, hive, registry.member_name())
