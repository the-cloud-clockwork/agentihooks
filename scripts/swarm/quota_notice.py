from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from scripts.inbox.store import InboxStore, Item
from scripts.swarm import capacity
from scripts.tick import pool

if TYPE_CHECKING:
    from scripts.swarm.store import RedisStore, SwarmConfig
    from scripts.swarm.tick import Ledger, Runtime

HURRY = "Your account has fifteen percent or less quota left. Finish the current step and push."
HANDOFF_OPENING = "Your account is at the quota handoff threshold."
WARNING_OPENING = "QUOTA HANDOFF WARNING:"
STALE_S = 180
HANDOFF = HANDOFF_OPENING + (
    " Commit, push, write the Handoff v2 document, "
    "then run agentihooks swarm {slug} handoff <doc> --reason quota and stop. "
    "A successor on another account will take your seat."
)


def level(account: dict) -> str:
    five = None if account["harness"] == "codex" else account["five_left"]
    week = account["week_left"]
    if (five is not None and five <= 5) or (week is not None and week <= 2):
        return "handoff"
    if (five is not None and five <= 15) or (week is not None and week <= 15):
        return "hurry"
    return ""


def is_notice(item: Item) -> bool:
    return item.sender == "swarm" and (item.text == HURRY or item.text.startswith((HANDOFF_OPENING, WARNING_OPENING)))


def apply(slug: str, store: RedisStore, decision: dict[str, Any]) -> list[str]:
    """Send each working lane agent the quota notice its account level calls for, fanned out over the pass workers."""
    accounts = {(row["harness"], row["name"]): row for row in decision.get("accounts", [])}
    key = store.key(slug, "quota-notices")

    def one(agent: Any) -> list[str]:
        """Return the notice action sent to ``agent``, if any."""
        if agent.lane not in capacity.LANES or agent.state != "working":
            return []
        row = accounts.get((agent.harness, agent.account))
        if row is None or row["state"] == "UNKNOWN":
            return []
        kind = level(row)
        life = f"{agent.name}:{agent.started_at}"
        previous = store.redis.hget(key, life)
        if (
            not kind
            or previous == kind
            or previous == "handoff"
            or store.redis.hget(store.key(slug, "quota-warning-lives"), agent.name) == str(agent.started_at)
        ):
            return []
        text = HANDOFF.format(slug=slug) if kind == "handoff" else HURRY
        InboxStore(store.redis).send("swarm", agent.name, text)
        store.redis.hset(key, life, kind)
        return [f"sent {agent.name} the quota {kind}"]

    return [action for found in pool.fan_out(one, store.agents(slug)) for action in found]


def refresh(
    slug: str, config: SwarmConfig, store: RedisStore, ledger: Ledger, runtime: Runtime, now_ms: int
) -> list[str]:
    from scripts.swarm import quota_handoff

    actions = capacity.apply(slug, config, store, ledger, runtime, now_ms)
    decision = dict[str, Any](capacity.read(store, slug))
    return (
        actions
        + quota_handoff.warn(slug, store, dict(os.environ))
        + apply(slug, store, decision)
        + stale(decision, now_ms)
    )


def stale(decision: dict[str, Any], now_ms: int) -> list[str]:
    """Name the newest quota reading's age once it passes ``STALE_S``, which means the quota prober stopped."""
    rows: list[dict[str, Any]] = decision.get("accounts", [])
    seen = [float(row["observed_at"]) for row in rows if row.get("observed_at") is not None]
    age = int(now_ms / 1000 - max(seen)) if seen else 0
    return [f"quota readings are {age} seconds old; the quota prober has not refreshed them"] if age > STALE_S else []
