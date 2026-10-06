"""Activity heartbeat of a swarm agent's session, read by the swarm tick before it counts the agent idle."""

import os
import time

from scripts.swarm import idle

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


def heard(prompt, environ=None, redis=None, now_ms=None):
    """Record a prompt the operator sent, so the inbox wake pass leaves the pane alone for its quiet window."""
    from scripts.inbox.wake import WAKE_TEXT
    from scripts.swarm.tick import NUDGE

    env = os.environ if environ is None else environ
    slug, name = env.get("AGENTIHOOKS_SWARM"), env.get("AGENTIHOOKS_AGENT_NAME")
    text = prompt.strip()
    if not (slug and name) or text in (WAKE_TEXT, NUDGE.format(slug=slug)) or text.startswith(NOTIFICATION):
        return False
    if redis is None:
        from scripts.swarm.store import redis_client

        redis = redis_client(env)
    idle.prompted(redis, slug, name, time.time_ns() // 1_000_000 if now_ms is None else now_ms)
    return True
