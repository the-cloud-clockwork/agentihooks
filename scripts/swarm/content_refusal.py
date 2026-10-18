from scripts.handoff import transfers
from scripts.swarm import idle
from scripts.swarm.store import MASTER


def repeated(store, slug, agent, observed) -> bool:
    if agent.harness != "codex" or agent.lane == MASTER or observed.state != "idle" or not observed.content_refusal:
        return False
    if store.claimant(slug, agent.task) != agent.name:
        return False
    key = store.key(slug, "content-refusals", agent.task)
    receipt = f"{agent.name}:{idle.turn(store.redis, slug, agent.name)}"
    store.redis.hsetnx(key, receipt, agent.name)
    return store.redis.hlen(key) >= 2


def handoff(store, slug, agent, now_ms) -> dict:
    text = store.handoff(slug, agent.task) or transfers.last_handoff(store, slug, agent.task)
    if not text:
        text = f"""## Intent
Continue task {agent.task} after repeated provider content refusals.

## Done
Observed: the tick recorded two distinct refusal endings for this task before retiring {agent.name}.

## Stopped at
Unknown: the refused turn left no agent handoff. Read the recorded task progress and proof before continuing.

## Decisions and promises
Continue through another available harness because Codex refused two turns on this task.

## Next
Read the task progress and proof, then continue the task from its last verified step.

## Read first
workspace:{agent.task}/progress
workspace:{agent.task}/proof

<!-- handoff complete -->
"""
    return transfers.record(store, slug, agent, "content-refusal", text, now_ms, "codex")
