"""A master pane never blocks on a question prompt: one left open ten minutes moves to the operator's Priorities as a
ledger question, the prompt is dismissed and the master is told through its inbox."""

import hashlib
import re

from scripts.inbox import wake
from scripts.inbox.seats import seat_address
from scripts.inbox.store import InboxStore
from scripts.swarm.store import MASTER

WINDOW_MS = 10 * 60 * 1000
KEY = "master-prompt"
PERMISSION = re.compile(r"do you want to\b", re.I)
NOTICE = (
    "Your question prompt sat unanswered for ten minutes, so the swarm moved it to the operator's Priorities as a "
    "ledger question and dismissed it: {question} Do not ask it again. Work your inbox and Priorities, and act on "
    "his answer when it lands."
)


def run(slug, store, ledger, runtime, now_ms) -> list[str]:
    live, actions = runtime.live_names(), []
    for agent in store.agents(slug):
        if agent.lane != MASTER:
            continue
        key = store.key(slug, f"{KEY}:{agent.name}")
        absent = agent.name not in live or agent.state in {"finished", "starting"} or not agent.pane_id
        observed = None if absent else runtime.observe(agent)
        if observed is None or observed.state != "waiting" or not _question_prompt(observed.prompt_title):
            store.redis.delete(key)
            continue
        asked = _asked(observed)
        if store.redis.hget(key, "asked") != asked:
            store.redis.delete(key)
            store.redis.hset(key, mapping={"asked": asked, "since": now_ms})
            continue
        state = store.redis.hgetall(key)
        if now_ms - int(state["since"]) < WINDOW_MS:
            continue
        op_id = hashlib.sha256(f"{slug}:{agent.name}:{state['since']}:{asked}".encode()).hexdigest()[:32]
        if "copied" not in state:
            ledger.question(slug, _question(observed, agent.name), "swarm", op_id)
            store.redis.hset(key, "copied", now_ms)
        if refused := runtime.dismiss_prompt(agent):
            actions.append(f"could not dismiss the question prompt on {agent.name}: {refused}, retrying next tick")
            continue
        if "moved" in state:
            continue
        InboxStore(store.redis).send(
            wake.BY,
            agent.seat or seat_address(slug, MASTER),
            NOTICE.format(question=observed.prompt_title),
            item_id=f"{KEY}-{op_id}",
        )
        store.redis.hset(key, "moved", now_ms)
        actions.append(f"moved the question prompt open ten minutes on {agent.name} to Priorities and freed its pane")
    return actions


def _question_prompt(title: str) -> bool:
    return bool(title) and not PERMISSION.match(title)


def _asked(observed) -> str:
    return "\n".join((observed.prompt_title, *observed.prompt_options))


def _question(observed, name) -> str:
    options = f" Options: {'; '.join(observed.prompt_options)}." if observed.prompt_options else ""
    return f"{observed.prompt_title}{options} Asked by the master {name}."
