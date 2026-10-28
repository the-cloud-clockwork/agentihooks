"""The questions a master's open question prompt carries, recorded by the question gate when the prompt opens, so the
tick can dismiss a prompt the operator left unanswered past the window and move its questions to his Priorities."""

import hashlib
import json
from dataclasses import replace

from hooks.context.master_question import WINDOW_SEC
from scripts.swarm import input_wait

DAY_S = 86_400


def _key(store, slug, name):
    return store.key(slug, "master-questions", name)


def hold(store, slug: str, name: str, questions: list[str]) -> None:
    store.redis.set(_key(store, slug, name), json.dumps(questions), ex=DAY_S)


def held(store, slug: str, name: str) -> list[str]:
    raw = store.redis.get(_key(store, slug, name))
    return json.loads(raw) if raw else []


def drop(store, slug: str, name: str) -> None:
    store.redis.delete(_key(store, slug, name))


def dismiss(slug, store, ledger, runtime, agent, now_ms: int) -> list[str]:
    """Dismiss a master prompt the gate recorded once it has been open past the window, writing its questions to the
    ledger first so a failed dismissal retries under the same ledger ids."""
    since = input_wait.since(store, slug, agent.name)
    if since is None or now_ms - since < WINDOW_SEC * 1000:
        return []
    questions = held(store, slug, agent.name)
    if not any(agent.input_prompt in question for question in questions):
        return []
    for question in questions:
        op_id = hashlib.sha256(f"{agent.name}:{since}:{question}".encode()).hexdigest()[:32]
        ledger.question(slug, question, agent.name, op_id)
    if not runtime.dismiss(agent):
        return [f"could not dismiss the question prompt of {agent.name}, retrying next tick"]
    drop(store, slug, agent.name)
    input_wait.clear(store, slug, agent.name)
    store.put_agent(slug, replace(agent, input_prompt="", input_ticks=0))
    return [
        f"dismissed the question prompt of {agent.name} after ten minutes and moved {len(questions)} questions to "
        "Priorities"
    ]
