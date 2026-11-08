"""The tick's hive sweep: an ephemeral hive quiet past the registry window is drained, then its records removed."""

from typing import Any

from scripts.hive import purge, registry

OWNER = "tick"
REASON = "quiet past the hive sweep window"


def swept(record: dict[str, Any], now_ms: int) -> bool:
    """An ephemeral hive that hosts no operator interface and has gone quiet is due for removal."""
    return record.get("ephemeral") == "yes" and record.get("ui") != "yes" and registry.quiet(record, now_ms)


def sweep(store: Any, now_ms: int) -> list[str]:
    """Drain each due hive, then delete its registry records once no agent record still runs on it."""
    lines: list[str] = []
    for record in registry.hives(store.redis):
        if not swept(record, now_ms):
            continue
        hive: str = record["id"]
        if record.get("draining") != "yes":
            registry.update(store.redis, hive, ["draining=yes"], drain=registry.Drain(OWNER, REASON))
            lines.append(f"drained quiet ephemeral hive {hive}")
        if running := purge.hosted(store, hive):
            lines.append(f"kept quiet hive {hive} while {len(running)} agent records still run on it")
            continue
        lines += purge.forget(store.redis, hive)
    return lines
