import json
import subprocess

from scripts.swarm import idle, ledger_events, merge_queue, waits
from scripts.swarm.store import PREFIX, AgentRecord, RedisStore, SwarmError


def key(slug: str) -> str:
    return f"{PREFIX}:{slug}:prerequisite-waits"


def admit(store: RedisStore, slug: str, agent: AgentRecord, held: dict, ledger, at: int) -> None:
    store.redis.hdel(key(slug), agent.task)
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
    idle.end_wait(store.redis, slug, agent.name, at)
    raise SwarmError(
        f"prerequisite {target} is at queue position {position}; stack the next change on the prerequisite branch "
        "and publish it, or block with this prerequisite URL, the stack and next step in the notes; "
        "the swarm will reopen after it merges"
    )


def offered(store: RedisStore, slug: str, agent: AgentRecord) -> dict:
    record = json.loads(store.redis.hget(key(slug), agent.task) or "{}")
    if record.get("agent") == agent.name and record.get("generation") == store.claim_generation(slug, agent.task):
        return record
    return {}


def dependencies(store: RedisStore, slug: str, agent: AgentRecord, task: dict, note: str) -> list:
    record = offered(store, slug, agent)
    return [] if record and record["target"] in waits.PULL_URL.findall(note) else task.get("depends_on", [])


def hold(store: RedisStore, slug: str, agent: AgentRecord, ledger, note: str) -> None:
    record = offered(store, slug, agent)
    if not record or record["target"] not in waits.PULL_URL.findall(note):
        return
    task = ledger.task(slug, agent.task)
    record.update(blocked=True, generation=task.get("claim_generation", 0))
    store.redis.hset(key(slug), agent.task, json.dumps(record))


def reopen_pass(slug: str, store: RedisStore, ledger, rows: dict) -> list[str]:
    actions = []
    for task_id, raw in store.redis.hgetall(key(slug)).items():
        record = json.loads(raw)
        if not record.get("blocked"):
            continue
        task = rows.get(task_id, {})
        expected = {"claimed_by": record["agent"], "claim_generation": record["generation"]}
        if task.get("state") != "blocked" or any(
            task.get(field, 0 if field == "claim_generation" else "") != value for field, value in expected.items()
        ):
            store.redis.hdel(key(slug), task_id)
            continue
        pull = ledger_events.view(record["target"])
        if pull is None or pull.state != "MERGED":
            continue
        live = ledger.update_task(
            slug, task_id, {"state": "open", "claimed_by": ""}, if_state=("blocked",), if_match=expected
        )
        rows[task_id].update(live)
        store.redis.hdel(key(slug), task_id)
        if live.get("state") == "open":
            ledger.comment(
                slug,
                task_id,
                "The queued prerequisite merged. Resume the published stack and next step from the block notes.",
                by="swarm",
            )
            actions.append(f"task {task_id} reopened after prerequisite {record['target']} merged")
    return actions
