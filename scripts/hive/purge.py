"""The registry step of hive remove: the agents a hive still runs, and deleting every registry record it holds."""

from typing import TYPE_CHECKING, Any

from scripts.hive import hive_state, registry
from scripts.swarm.keyspace import ROOT

if TYPE_CHECKING:
    from scripts.swarm.store import AgentRecord

ENDED = ("finished", "ended")


def hosted(store: Any, hive: str) -> list[tuple[str, "AgentRecord"]]:
    """Every agent record of any swarm placed on ``hive`` that has not finished or ended."""
    return [
        (slug, agent)
        for slug in store.slugs()
        for agent in store.agents(slug)
        if agent.hive == hive and agent.state not in ENDED
    ]


def keys(redis: Any, hive: str) -> list[str]:
    """The legacy registry hash of ``hive`` and every key under it."""
    return [registry.key(hive), *redis.scan_iter(match=f"{registry.key(hive)}:*")]


def recorded(redis: Any, hive: str) -> bool:
    """Whether the hive state store holds a record of ``hive``."""
    return hive_state.read(hive_state.records(redis, hive), hive) is not None


def forget(redis: Any, hive: str) -> list[str]:
    """Delete every registry record of ``hive`` and unindex it, keeping its agents' wait histories under the swarm."""
    found: list[str] = keys(redis, hive)
    prefix = f"{registry.key(hive)}:swarm:"
    histories: dict[str, dict[str, str]] = {
        f"{ROOT}:swarm:{source.removeprefix(prefix)}": redis.hgetall(source)
        for source in found
        if source.startswith(prefix) and ":wait-history:" in source
    }
    with redis.pipeline() as pipe:
        for target, history in histories.items():
            pipe.hset(target, mapping=history)
        pipe.delete(*found)
        deleted: int = pipe.execute()[-1]
    if recorded(redis, hive):
        registry.forget(redis, hive)
        deleted += 1
    hive_state.indexed(redis, hive, False)
    return [f"deleted {deleted} registry records of {hive}"]
