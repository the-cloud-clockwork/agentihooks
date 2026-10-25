import json
import subprocess
from argparse import ArgumentParser

from scripts.swarm import idle, ledger_events, merge_queue, waits
from scripts.swarm.store import PREFIX, AgentRecord, RedisStore, SwarmError


def key(slug: str) -> str:
    return f"{PREFIX}:{slug}:prerequisite-waits"


def arguments(parser: ArgumentParser) -> None:
    parser.add_argument("--on", nargs=2, metavar=("KIND", "TARGET"))
    parser.add_argument("--final", action="store_true")


def admit(store: RedisStore, slug: str, agent: AgentRecord, held: dict, ledger, at: int, final: bool) -> None:
    store.redis.hdel(key(slug), agent.task)
    if held["kind"] != "merge":
        return
    task = ledger.task(slug, agent.task) if agent.task else None
    target = held["target"]
    if task and task.get("claimed_by") != agent.name:
        raise SwarmError("task claim changed; refresh the task before waiting")
    if final:
        if not task or task.get("pr_url") != target:
            raise SwarmError("--final requires the current task pull request")
        return
    if not task:
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
        json.dumps({"agent": agent.name, "generation": task.get("claim_generation", 0), "target": target}),
    )
    idle.end_wait(store.redis, slug, agent.name, at)
    raise SwarmError(
        f"prerequisite {target} is at queue position {position}; stack the next change on the prerequisite branch "
        "and publish it, or block with this prerequisite URL, the stack and next step in the notes; "
        "the swarm will reopen after it merges. For the task's final deliverable, "
        f"confirm it with wait --on merge {target} --final"
    )


def offered(store: RedisStore, slug: str, agent: AgentRecord, task: dict | None) -> dict:
    record = json.loads(store.redis.hget(key(slug), agent.task) or "{}")
    if record.get("agent") == agent.name and record.get("generation") == (task or {}).get("claim_generation", 0):
        return record
    return {}


def dependencies(store: RedisStore, slug: str, agent: AgentRecord, task: dict, note: str) -> list:
    record = offered(store, slug, agent, task)
    return [] if record and record["target"] in waits.PULL_URL.findall(note) else task.get("depends_on", [])


def hold(store: RedisStore, slug: str, agent: AgentRecord, ledger, note: str) -> None:
    if json.loads(store.redis.hget(key(slug), agent.task) or "{}").get("target") not in waits.PULL_URL.findall(note):
        return
    record = offered(store, slug, agent, ledger.task(slug, agent.task))
    if not record:
        return
    record["blocked"] = True
    store.redis.hset(key(slug), agent.task, json.dumps(record))


def reopen_pass(slug: str, store: RedisStore, ledger, rows: dict) -> list[str]:
    actions = []
    for task_id, raw in store.redis.hgetall(key(slug)).items():
        record = json.loads(raw)
        if not record.get("blocked"):
            continue
        task = rows.get(task_id, {})
        expected = {"claimed_by": record["agent"], "claim_generation": record["generation"]}
        if task.get("state") != "blocked" or any(task.get(field, 0) != value for field, value in expected.items()):
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
