"""Compact verified hive members without changing legacy token authentication."""

from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager
from functools import partial
from itertools import islice
from typing import TYPE_CHECKING, Any, cast

from scripts.events.redis_state import HISTORY, RedisStateStore
from scripts.hive import auth, hive_state

if TYPE_CHECKING:
    from scripts.swarm.store import AgentRecord


def _without_agents(current: dict[str, Any]) -> dict[str, Any]:
    return {field: value for field, value in current.items() if field != "agents"}


@contextmanager
def _member_guard(client: Any, member_id: str) -> Generator[None]:
    with client.lock(f"{auth.PREFIX}:credential-maintenance:{member_id}", timeout=600, blocking_timeout=10):
        yield


def _without_scope(current: dict[str, Any] | None, slug: str, name: str) -> Any:
    if current is None or "agents" not in current:
        return hive_state.KEEP
    kept = {digest: scope for digest, scope in current["agents"].items() if scope != [slug, name]}
    return hive_state.KEEP if kept == current["agents"] else {**current, "agents": kept}


def _compact_member(client: Any, member_id: str) -> int:
    store = hive_state.credentials(client, auth.PREFIX)
    member = auth.member(client, member_id)
    if member is None:
        raise auth.HiveError(f"unknown hive member {member_id}")
    if "agents" in member:
        for digest, scope in member["agents"].items():
            record = {"ledger": member["ledger"], "scope": scope}
            hive_state.amend(store, f"agent.{member_id}.{digest}", lambda current: current or record)
        _index_legacy(client, member_id)
        for _ in range(HISTORY):
            member = hive_state.amend(store, f"member.{member_id}", _without_agents)
    return len(json.dumps(member).encode())


def compact(client: Any, members: list[str], readers_compatible: bool = False) -> dict[str, int]:
    """Compact only explicitly verified members and report document bytes without credential values."""
    if not readers_compatible or not members:
        raise auth.HiveError("compaction requires verified readers and an explicit member list")
    if not hive_state.on_redis():
        raise auth.HiveError("credential compaction requires the Redis state backend")
    sizes: dict[str, int] = {}
    for member_id in members:
        with _member_guard(client, member_id):
            sizes[member_id] = _compact_member(client, member_id)
    return sizes


def _index_key(member_id: str, slug: str, name: str) -> str:
    return hive_state.agent_key(member_id, slug, name).replace("agent.", "agent-index.")


def _index_legacy(client: Any, member_id: str) -> None:
    store = cast(RedisStateStore, hive_state.credentials(client, auth.PREFIX))
    keys = (key for key, _ in client.hscan_iter(store.watch_key, match=f"agent.{member_id}.*", count=128))
    grouped: dict[tuple[str, str], list[str]] = {}
    while batch := list(islice(keys, 128)):
        for entry in store.get_many(batch):
            if entry is None or not entry.value:
                continue
            record: dict[str, Any] = json.loads(entry.value)
            if "tokens" not in record:
                slug, name = record["scope"]
                grouped.setdefault((slug, name), []).append(entry.key)
    for (slug, name), held in grouped.items():
        hive_state.amend(
            store, _index_key(member_id, slug, name), lambda current: sorted(set(current or ()) | set(held))
        )


def retire(client: Any, member_id: str, slug: str, name: str) -> None:
    """Remove one ended agent's current and legacy credentials under the compaction fence."""
    if not member_id:
        return
    with _member_guard(client, member_id):
        store = hive_state.credentials(client, auth.PREFIX)
        member = auth.member(client, member_id)
        if member and "agents" in member and hive_state.on_redis():
            _index_legacy(client, member_id)
        index = _index_key(member_id, slug, name)
        for key in hive_state.read(store, index) or ():
            hive_state.amend(store, key, lambda _: None)
        hive_state.amend(store, hive_state.agent_key(member_id, slug, name), lambda _: None)
        hive_state.amend(store, index, lambda _: None)
        hive_state.amend(store, f"member.{member_id}", partial(_without_scope, slug=slug, name=name))


def retire_named(client: Any, slug: str, name: str, previous: str | None, lives: list[AgentRecord]) -> None:
    """Remove the retired name's credentials from every recorded hive placement."""
    members = {agent.hive for agent in lives if agent.name == name and agent.hive}
    if previous:
        member_id = json.loads(previous).get("hive")
        if member_id:
            members.add(member_id)
    for member_id in members:
        retire(client, member_id, slug, name)
