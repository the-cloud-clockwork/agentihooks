"""Activity heartbeat of a swarm agent's session, read by the swarm tick before it counts the agent idle."""

import os
import time

from scripts.swarm import idle, session_model

NOTIFICATION = "<task-notification>"
CHANNEL = "<channel source="
PEER = ("Another Claude session sent a message:", "<cross-session-message ", "<agent-message ")


def beat(state, environ=None, redis=None, now_ms=None):
    env = os.environ if environ is None else environ
    slug, name = env.get("AGENTIHOOKS_SWARM", ""), env.get("AGENTIHOOKS_AGENT_NAME", "")
    if not (slug and name):
        return False
    if redis is None:
        from scripts.swarm.store import redis_client

        redis = redis_client(env)
    idle.beat(redis, slug, name, state, time.time_ns() // 1_000_000 if now_ms is None else now_ms)
    if state == idle.IDLE:
        from scripts.swarm.health import activity

        activity.end_turn(slug, name)
    return True


def is_operator_prompt(prompt, slug):
    """False for the prompts the swarm or another session puts into a pane: marked deliveries, inbox wakes, idle nudges, task notifications, channel items, cross session messages."""
    from scripts.inbox.wake import WAKE_TEXT
    from scripts.swarm.delivery import MARK
    from scripts.swarm.tick import NUDGE

    text = prompt.strip()
    return (
        bool(text)
        and text not in (WAKE_TEXT, NUDGE.format(slug=slug))
        and not text.startswith((NOTIFICATION, CHANNEL, MARK, *PEER))
    )


def heard(prompt, environ=None, redis=None, now_ms=None):
    """Record a prompt the operator sent, so the inbox wake pass leaves the pane alone for its quiet window."""
    env = os.environ if environ is None else environ
    slug, name = env.get("AGENTIHOOKS_SWARM"), env.get("AGENTIHOOKS_AGENT_NAME")
    if not (slug and name):
        return False
    if redis is None:
        from scripts.swarm.store import redis_client

        redis = redis_client(env)
    idle.begin_turn(redis, slug, name)
    if not is_operator_prompt(prompt, slug):
        return False
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


def outcome(payload, environ=None, redis=None, now_ms=None):
    """Record a push or an opened pull request as the pinned worker's outcome, on every harness."""
    env = os.environ if environ is None else environ
    slug, name = env.get("AGENTIHOOKS_SWARM"), env.get("AGENTIHOOKS_AGENT_NAME")
    if not (slug and name):
        return False
    from scripts.gates.progress import Progress, outcome_of

    command = (payload.get("tool_input") or {}).get("command") if payload.get("tool_name") == "Bash" else None
    kind = outcome_of(command)
    if not kind:
        return False
    if redis is None:
        from scripts.swarm.store import redis_client

        redis = redis_client(env)
    Progress(redis, slug).outcome(name, kind, now_ms)
    return True


def push_mark(payload, environ=None, redis=None, now_ms=None):
    """Mark a pinned worker's push of its task branch or queue of its pull request, for the tick's checked wait."""
    env = os.environ if environ is None else environ
    slug, name = env.get("AGENTIHOOKS_SWARM"), env.get("AGENTIHOOKS_AGENT_NAME")
    command = (payload.get("tool_input") or {}).get("command") if payload.get("tool_name") == "Bash" else None
    from scripts.swarm import pr_wait

    if not (slug and name and env.get("AGENTIHOOKS_SWARM_TASK") and pr_wait.triggered(command)):
        return False
    if redis is None:
        from scripts.swarm.store import redis_client

        redis = redis_client(env)
    repo, branch = pr_wait.pushed_where(command, payload.get("cwd") or ".")
    at = int(time.time() * 1000) if now_ms is None else now_ms
    pr_wait.mark(redis, slug, name, at, repo, branch)
    return True
