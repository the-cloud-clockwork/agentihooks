from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

from scripts.inbox import exits
from scripts.inbox.seats import seat_address
from scripts.inbox.store import InboxStore
from scripts.swarm.store import MASTER, RedisStore

if TYPE_CHECKING:
    from scripts.swarm.tick import Ledger, Runtime


def retire_idle_master(
    slug: str, store: RedisStore, ledger: Ledger, runtime: Runtime, rows: dict, now_ms: int
) -> list[str]:
    agents = store.agents(slug)
    if not agents or any(a.lane != MASTER for a in agents):
        return []
    inbox = InboxStore(store.redis)
    events = ledger.events(slug)
    last_event = max((e.get("at", 0) for e in events), default=0)
    limit = float(os.environ.get("AGENTIHOOKS_MASTER_IDLE_HOURS", "6")) * 60 * 60 * 1000
    actions = []
    for agent in agents:
        seat = agent.seat or seat_address(slug, MASTER)
        if now_ms - max(agent.started_at, last_event) <= limit:
            continue
        if runtime.status(agent) not in {"idle", "done"}:
            continue
        if inbox.pending_items(seat) or inbox.pending_items(agent.name):
            continue
        if not runtime.retire(agent, agent.name in runtime.live_names()):
            actions.append(f"could not retire {agent.name}, retrying next tick")
            continue
        store.memory.add_recap(
            seat,
            agent.name,
            MASTER,
            "Master retired after the swarm idle limit; no workers or pending messages remained.",
            now_ms,
        )
        exits.settle(inbox, agent.name, seat, "retired after the swarm idle limit")
        store.drop_agent(slug, agent.name)
        store.redis.set(store.key(slug, "master-retired-tasks"), json.dumps(list(rows)))
        actions.append(f"retired {agent.name} after the swarm idle limit")
    return actions


def sleeping(slug: str, store: RedisStore, rows: dict) -> bool:
    previous = store.redis.get(store.key(slug, "master-retired-tasks"))
    if previous is None:
        return False
    if set(rows).difference(json.loads(previous)):
        store.redis.delete(store.key(slug, "master-retired-tasks"))
        return False
    return not InboxStore(store.redis).pending_items(seat_address(slug, MASTER))
