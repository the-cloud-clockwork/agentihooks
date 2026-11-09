"""Compact verified hive members without changing legacy token authentication."""

import json
from typing import Any

from scripts.events.redis_state import HISTORY
from scripts.hive import auth, hive_state


def _without_agents(current: dict[str, Any]) -> dict[str, Any]:
    return {field: value for field, value in current.items() if field != "agents"}


def _compact_member(client: Any, member_id: str) -> int:
    store = auth._store(client)
    member = auth.member(client, member_id)
    if member is None:
        raise auth.HiveError(f"unknown hive member {member_id}")
    if "agents" in member:
        for digest, scope in member["agents"].items():
            record = {"ledger": member["ledger"], "scope": scope}
            hive_state.amend(store, f"agent.{member_id}.{digest}", lambda current: current or record)
        for _ in range(HISTORY):
            member = hive_state.amend(store, f"member.{member_id}", _without_agents)
    return len(json.dumps(member).encode())


def compact(client: Any, members: list[str], readers_compatible: bool = False) -> dict[str, int]:
    """Compact only explicitly verified members and report document bytes without credential values."""
    if not readers_compatible or not members:
        raise auth.HiveError("compaction requires verified readers and an explicit member list")
    if not hive_state.on_redis():
        raise auth.HiveError("credential compaction requires the Redis state backend")
    return {member_id: _compact_member(client, member_id) for member_id in members}
