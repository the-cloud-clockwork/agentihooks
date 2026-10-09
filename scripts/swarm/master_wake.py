import os
from typing import TYPE_CHECKING

from scripts.inbox import wake
from scripts.inbox.seats import seat_address
from scripts.inbox.store import InboxStore
from scripts.swarm import idle
from scripts.swarm.store import MASTER, RedisStore

if TYPE_CHECKING:
    from scripts.swarm.tick import Runtime

WINDOW_MS = 20 * 60 * 1000
PROMPT = "Swarm backstop: run agentihooks msg inbox and work your Priorities. Work is waiting while your pane is idle."


def waiting(doc: dict) -> bool:
    return (
        any(not p.get("cleared") for p in doc.get("priorities", []))
        or any(
            not f.get("done") and not f.get("deleted") and not f.get("out_of_scope") for f in doc.get("followups", [])
        )
        or any(t.get("state") == "blocked" and not t.get("out_of_scope") for t in doc.get("tasks", []))
    )


def run(slug: str, store: RedisStore, runtime: "Runtime", doc: dict, now_ms: int) -> list[str]:
    live = runtime.live_names()
    inbox = InboxStore(store.redis)
    actions = []
    for agent in store.agents(slug):
        if agent.lane != MASTER:
            continue
        key = store.key(slug, f"master-wake:{agent.name}")
        if agent.name not in live or agent.state in {"finished", "starting"} or not agent.pane_id:
            store.redis.delete(key)
            continue
        observed = runtime.observe(agent)
        if observed.state != "idle":
            store.redis.delete(key)
            continue
        store.redis.hsetnx(key, "idle_since", now_ms)
        state = store.redis.hgetall(key)
        if now_ms - int(state["idle_since"]) < WINDOW_MS:
            continue
        if "woken_at" in state and now_ms - int(state["woken_at"]) < WINDOW_MS:
            continue
        last = idle.last_prompt(store.redis, slug, agent.name)
        if observed.typed or (last is not None and now_ms - last < wake.quiet_ms(os.environ)):
            continue
        if not (
            waiting(doc) or inbox.open_items(agent.name) or inbox.open_items(agent.seat or seat_address(slug, MASTER))
        ):
            continue
        runtime.nudge(agent, PROMPT)
        store.redis.hset(key, "woken_at", now_ms)
        actions.append(f"woke idle master {agent.name} to work waiting Priorities")
    return actions
