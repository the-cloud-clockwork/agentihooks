from __future__ import annotations

import os
from typing import TYPE_CHECKING

from scripts.inbox.store import InboxStore
from scripts.swarm import capacity

if TYPE_CHECKING:
    from scripts.swarm.store import RedisStore, SwarmConfig
    from scripts.swarm.tick import Ledger, Runtime

HURRY = "Your account has fifteen percent or less quota left. Finish the current step and push."
HANDOFF = (
    "Your account is at the quota handoff threshold. Commit, push, write the Handoff v2 document, "
    "then run agentihooks swarm {slug} handoff <doc> --reason quota and stop. "
    "A successor on another account will take your seat."
)


def level(account: dict) -> str:
    five, week = account["five_left"], account["week_left"]
    if (five is not None and five <= 5) or (week is not None and week <= 2):
        return "handoff"
    if (five is not None and five <= 15) or (week is not None and week <= 15):
        return "hurry"
    return ""


def apply(slug: str, store: RedisStore, decision: dict) -> list[str]:
    accounts = {(row["harness"], row["name"]): row for row in decision.get("accounts", [])}
    key = store.key(slug, "quota-notices")
    actions = []
    for agent in store.agents(slug):
        if agent.lane not in capacity.LANES or agent.state != "working":
            continue
        row = accounts.get((agent.harness, agent.account))
        if row is None or row["state"] == "UNKNOWN":
            continue
        kind = level(row)
        life = f"{agent.name}:{agent.started_at}"
        previous = store.redis.hget(key, life)
        if (
            not kind
            or previous == kind
            or previous == "handoff"
            or store.redis.hget(store.key(slug, "quota-warning-lives"), agent.name) == str(agent.started_at)
        ):
            continue
        text = HANDOFF.format(slug=slug) if kind == "handoff" else HURRY
        InboxStore(store.redis).send("swarm", agent.name, text)
        store.redis.hset(key, life, kind)
        actions.append(f"sent {agent.name} the quota {kind}")
    return actions


def refresh(
    slug: str, config: SwarmConfig, store: RedisStore, ledger: Ledger, runtime: Runtime, now_ms: int
) -> list[str]:
    from scripts.swarm import quota_handoff

    actions = capacity.apply(slug, config, store, ledger, runtime, now_ms)
    return actions + quota_handoff.warn(slug, store, dict(os.environ)) + apply(slug, store, capacity.read(store, slug))
