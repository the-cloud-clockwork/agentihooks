import json

from scripts.inbox.store import InboxStore
from scripts.swarm import idle, outage

LIMIT_MS = 30 * 60 * 1000
IDLE_ANSWER = "nothing to do"
REPLY_PREFIX = "done: replied with message "
TEXT = (
    "You are an automated ledger agent and have been inactive for thirty minutes. "
    'What are you doing? Reply to this message with exactly "nothing to do" if you have no work, '
    "otherwise describe your work. Three nothing to do answers end your agent and close its pane."
)


def owned(slug: str, agent) -> bool:
    return bool(agent.seat) and agent.profile_decision.get("automated") == {"swarm": slug, "seat": agent.seat}


def _key(store, slug, agent):
    return store.key(slug, "automated-gc", agent.execution_id or f"{agent.name}:{agent.started_at}")


def state(store, slug: str, agent) -> dict:
    raw = store.redis.get(_key(store, slug, agent))
    return json.loads(raw) if raw else {"idle_answers": 0, "answers": [], "request": "", "last_probe": 0}


def _save(store, slug, agent, record):
    store.redis.set(_key(store, slug, agent), json.dumps(record))


def _answer(inbox, agent, record, now_ms):
    request = inbox.get(record["request"])
    if not request.reason.startswith(REPLY_PREFIX):
        return False
    answer = inbox.get(request.reason.removeprefix(REPLY_PREFIX))
    if answer.ref != request.id or not inbox.acts_for(answer.sender, agent.name):
        return False
    record["answers"].append({"id": answer.id, "text": answer.text, "at": now_ms})
    record["answers"] = record["answers"][-3:]
    record["idle_answers"] = record["idle_answers"] + 1 if answer.text == IDLE_ANSWER else 0
    record["request"] = ""
    inbox.close(answer.id, "swarm", "done", "activity answer recorded")
    return True


def _ready(store, slug, runtime, agent, now_ms):
    if outage.held(store, slug, agent.name):
        return False
    observed = runtime.observe(agent)
    if observed.typed or observed.prompt_title or observed.provider_error:
        return False
    return (
        idle.verdict(
            observed.state,
            idle.heartbeat(store.redis, slug, agent.name),
            idle.wait(store.redis, slug, agent.name),
            now_ms,
        )
        == idle.IDLE
    )


def run(slug: str, store, ledger, runtime, now_ms: int, release=None) -> list[str]:
    inbox, actions = InboxStore(store.redis), []
    for agent in store.agents(slug):
        if not owned(slug, agent):
            continue
        record = state(store, slug, agent)
        if record["request"] and _answer(inbox, agent, record, now_ms):
            _save(store, slug, agent, record)
            actions.append(f"recorded activity answer from {agent.name}")
        if not _ready(store, slug, runtime, agent, now_ms):
            continue
        if record["idle_answers"] >= 3:
            if runtime.retire(agent):
                ledger.notify(slug, "An automated agent ended after three nothing to do answers.")
                if release is None:
                    store.release(slug, agent.task, agent.name)
                    store.drop_agent(slug, agent.name)
                else:
                    release(agent)
                actions.append(f"collected automated agent {agent.name} after three idle answers")
            continue
        beat = idle.heartbeat(store.redis, slug, agent.name) or {}
        active = max(agent.started_at, beat.get("at", 0), idle.last_prompt(store.redis, slug, agent.name) or 0)
        if record["request"] or now_ms - max(active, record["last_probe"]) < LIMIT_MS:
            continue
        item = inbox.send("swarm", agent.name, TEXT, ref="automated-activity", task=agent.task)
        record.update(request=item.id, last_probe=now_ms)
        _save(store, slug, agent, record)
        actions.append(f"asked automated agent {agent.name} what it is doing")
    return actions
