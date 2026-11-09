"""The tick's hive sweep: an ephemeral hive quiet past the registry window is drained, then its records removed."""

from typing import Any

from scripts.hive import hive_state, purge, registry

OWNER = "tick"
REASON = "quiet past the hive sweep window"


def swept(record: dict[str, Any], now_ms: int) -> bool:
    """An ephemeral hive that hosts no operator interface and has gone quiet is due for removal."""
    return record.get("ephemeral") == "yes" and record.get("ui") != "yes" and registry.quiet(record, now_ms)


def indexed(redis: Any) -> list[str]:
    """The hive ids the state index holds, read in one call without the legacy index set."""
    ids: list[str] = hive_state.read(hive_state.records(redis, hive_state.INDEX), hive_state.INDEX) or []
    return ids


def sweep(store: Any, now_ms: int) -> list[str]:
    """Drain each due hive, then delete its registry records once no agent record still runs on it."""
    lines: list[str] = []
    for hive_id in indexed(store.redis):
        record: dict[str, Any] | None = hive_state.read(hive_state.records(store.redis, hive_id), hive_id)
        if record is None or not swept(record, now_ms):
            continue
        if record.get("draining") != "yes":
            registry.update(store.redis, hive_id, ["draining=yes"], drain=registry.Drain(OWNER, REASON))
            lines.append(f"drained quiet ephemeral hive {hive_id}")
        if running := purge.hosted(store, hive_id):
            lines.append(f"kept quiet hive {hive_id} while {len(running)} agent records still run on it")
            continue
        lines += purge.forget(store.redis, hive_id)
    return lines
