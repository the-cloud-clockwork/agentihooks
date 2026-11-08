"""The tick's hive sweep: an ephemeral hive quiet past the registry window is drained, then its records removed."""

from typing import TYPE_CHECKING

from scripts.hive import registry, remove

if TYPE_CHECKING:
    from scripts.swarm.store import RedisStore

OWNER = "tick"
REASON = "quiet past the hive sweep window"


def swept(record: dict, now_ms: int) -> bool:
    """An ephemeral hive that hosts no operator interface and has gone quiet is due for removal."""
    return record["ephemeral"] == "yes" and record["ui"] != "yes" and registry.quiet(record, now_ms)


def sweep(store: "RedisStore", now_ms: int) -> list[str]:
    """Drain each due hive, then delete its registry records once no agent record still runs on it."""
    lines: list[str] = []
    for record in registry.hives(store.redis):
        if not swept(record, now_ms):
            continue
        hive = record["id"]
        if record.get("draining") != "yes":
            registry.update(store.redis, hive, ["draining=yes"], drain=registry.Drain(OWNER, REASON))
            lines.append(f"drained quiet ephemeral hive {hive}")
        if running := remove.hosted(store, hive):
            lines.append(f"kept quiet hive {hive} while {len(running)} agent records still run on it")
            continue
        lines += remove.forget(store.redis, hive)
    return lines
