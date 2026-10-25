from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

from scripts.handoff import transfers
from scripts.swarm import master_alarm, reaper
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_v2 import masters as master_seats

if TYPE_CHECKING:
    from scripts.swarm.tick import Ledger, Runtime

DEADLINE_MS = 2 * 60 * 1000
RETRY_MS = 5 * 60 * 1000
NO_HOOK = "master {name} reported no hook within two minutes of its launch"


def read(store: RedisStore, slug: str) -> dict:
    raw = store.redis.get(store.key(slug, "master-start"))
    return json.loads(raw) if raw else {}


def save(store: RedisStore, slug: str, pending: dict) -> None:
    store.redis.set(store.key(slug, "master-start"), json.dumps(pending))


def clear_unalerted(store: RedisStore, slug: str) -> None:
    if not read(store, slug).get("alerted"):
        store.redis.delete(store.key(slug, "master-start"))


def begin(store: RedisStore, slug: str, name: str, task: dict, at: int) -> None:
    pending = read(store, slug)
    state = {"name": name, "task": task, "attempt": pending.get("attempt", 0) + 1, "at": at}
    if pending.get("alerted"):
        state["alerted"] = True
    save(store, slug, state)


def blocked(pending: dict, at: int) -> bool:
    if pending.get("alerted"):
        return at - pending["at"] < RETRY_MS
    return bool(pending.get("name"))


def deadline_ms(agent: AgentRecord) -> int:
    """A deferred launch returns before its login, so its hook may come as late as the launch budget it carries."""
    return max(DEADLINE_MS, agent.profile_decision.get("validation", {}).get("deadline_ms", DEADLINE_MS))


def failed(store: RedisStore, slug: str, error: str, at: int) -> list[str]:
    pending = read(store, slug)
    repeated = pending.get("alerted") and master_alarm.read(store, slug).get("error") == error
    master_alarm.failed(store, slug, error, at)
    if pending.get("attempt") == 1:
        save(store, slug, {**pending, "name": "", "retry": True})
    return [] if repeated else [error]


def observe(slug: str, config: SwarmConfig, store: RedisStore, ledger: Ledger, runtime: Runtime, at: int) -> list[str]:
    pending = read(store, slug)
    if not pending:
        return []
    if config.state == "stopping":
        store.redis.delete(store.key(slug, "master-start"))
        return []
    records = [a for a in store.agents(slug) if master_seats.is_lead(slug, a)]
    masters = [a for a in records if a.state != "finished"]
    agent = next((a for a in masters if a.name == pending["name"]), None)
    if any(a.name != pending["name"] for a in masters) or any(
        a.name == pending["name"] and a.state == "finished" for a in records
    ):
        store.redis.delete(store.key(slug, "master-start"))
        return []
    if agent and runtime.reported(agent):
        store.put_agent(slug, replace(agent, state="working"))
        store.clear_handoff(slug, MASTER)
        store.redis.delete(store.key(slug, "master-start"))
        return []
    if (pending.get("alerted") and not agent) or at - pending["at"] < (deadline_ms(agent) if agent else DEADLINE_MS):
        return []
    if agent:
        master_alarm.failed(store, slug, NO_HOOK.format(name=agent.name), at)
    if agent and not runtime.retire(agent, homes=reaper.scratch_homes(slug, agent.task)):
        if not pending.get("retire_told"):
            save(store, slug, {**pending, "retire_told": True})
            ledger.notify(
                slug, "The master reported no hook and its launch could not be retired. Operator action is required."
            )
        return [f"could not retire unreported master {agent.name}, retrying next tick"]
    if agent:
        transfers.failed(store, slug, agent)
        store.drop_agent(slug, agent.name)
    if pending.get("alerted"):
        save(store, slug, {**pending, "at": at})
        return []
    if pending["attempt"] == 1 and not pending.get("retry"):
        save(store, slug, {**pending, "name": "", "retry": True, "at": at})
        return [f"lost {agent.name}"] if agent else ["master launch failed, retrying once"]
    save(store, slug, {**pending, "alerted": True, "at": at})
    ledger.notify(
        slug,
        "The master reported no hook within two minutes on either launch. The operator has been alerted; automatic retries continue every five minutes. The original handoff is retained.",
    )
    return ["master startup failed twice, raised to the operator"]
