import json
import subprocess

from scripts.swarm import ledger_events, merge_queue
from scripts.swarm.store import PREFIX, AgentRecord, RedisStore, SwarmError


def key(slug: str) -> str:
    return f"{PREFIX}:{slug}:prerequisite-waits"


def admit(store: RedisStore, slug: str, agent: AgentRecord, held: dict, ledger) -> None:
    if held["kind"] != "merge" or not agent.task:
        return
    task = ledger.task(slug, agent.task)
    target = held["target"]
    if not task or task.get("pr_url") == target:
        return
    pull = ledger_events.view(target)
    if pull is None or not pull.queued:
        return
    try:
        state = merge_queue.operate("state", target)
    except (SwarmError, OSError, subprocess.SubprocessError):
        return
    position = (state["entry"] or {}).get("position")
    if state["state"] != "OPEN" or not state["queued"] or position is None or position <= 5:
        return
    store.redis.hset(
        key(slug),
        agent.task,
        json.dumps({"agent": agent.name, "generation": store.claim_generation(slug, agent.task), "target": target}),
    )
    raise SwarmError(
        f"prerequisite {target} is at queue position {position}; stack the next change on the prerequisite branch "
        "and publish it, or block with the stack and next step in the notes; the swarm will reopen after it merges"
    )
