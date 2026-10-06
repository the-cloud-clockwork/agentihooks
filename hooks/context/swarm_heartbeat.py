"""Activity heartbeat of a swarm agent's session, read by the swarm tick before it counts the agent idle."""

import os
import time

from scripts.swarm import idle, session_model

NOTIFICATION = "<task-notification>"


def beat(state, environ=None, redis=None, now_ms=None):
    env = os.environ if environ is None else environ
    slug, name = env.get("AGENTIHOOKS_SWARM", ""), env.get("AGENTIHOOKS_AGENT_NAME", "")
    if not (slug and name):
        return False
    if redis is None:
        from scripts.swarm.store import redis_client

        redis = redis_client(env)
    idle.beat(redis, slug, name, state, time.time_ns() // 1_000_000 if now_ms is None else now_ms)
    return True


def is_operator_prompt(prompt, slug):
    """False for the prompts the swarm itself types into a pane: inbox wakes, idle nudges, task notifications."""
    from scripts.inbox.wake import WAKE_TEXT
    from scripts.swarm.tick import NUDGE

    text = prompt.strip()
    return bool(text) and text not in (WAKE_TEXT, NUDGE.format(slug=slug)) and not text.startswith(NOTIFICATION)


def heard(prompt, environ=None, redis=None, now_ms=None):
    """Record a prompt the operator sent, so the inbox wake pass leaves the pane alone for its quiet window."""
    env = os.environ if environ is None else environ
    slug, name = env.get("AGENTIHOOKS_SWARM"), env.get("AGENTIHOOKS_AGENT_NAME")
    if not (slug and name) or not is_operator_prompt(prompt, slug):
        return False
    if redis is None:
        from scripts.swarm.store import redis_client

        redis = redis_client(env)
    idle.prompted(redis, slug, name, time.time_ns() // 1_000_000 if now_ms is None else now_ms)
    return True


def report(model, effort="", environ=None, redis=None, now_ms=None):
    env = os.environ if environ is None else environ
    slug, name = env.get("AGENTIHOOKS_SWARM"), env.get("AGENTIHOOKS_AGENT_NAME")
    if not (slug and name and model):
        return False
    if redis is None:
        from scripts.swarm.store import redis_client

        redis = redis_client(env)
    session_model.put(redis, slug, name, model, effort, time.time_ns() // 1_000_000 if now_ms is None else now_ms)
    return True
