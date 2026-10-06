"""The model and effort a swarm agent's running session reports, which the tick copies onto its agent record."""

import json
import re
from dataclasses import replace

from scripts.swarm.store import PREFIX

SESSION = "session"
TTL_S = 24 * 3600


def key(slug, name):
    return ":".join((PREFIX, slug, "session-model", name))


def put(redis, slug, name, model, effort, at):
    from scripts.swarm.naming import NameRegistry

    name = NameRegistry(redis).resolve(name)
    redis.set(key(slug, name), json.dumps({"model": model, "effort": effort, "at": at}), ex=TTL_S)


def get(redis, slug, name):
    from scripts.swarm.naming import NameRegistry

    raw = redis.get(key(slug, NameRegistry(redis).resolve(name)))
    return json.loads(raw) if raw else None


def _family(model):
    return re.sub(r"-[\d.-]+$", "", re.sub(r"\[.*\]$", "", model.removeprefix("claude-")))


def apply(agent, reported):
    """The agent record with the reported model and effort; unchanged when the report predates the agent's start or
    names what the record already says, a launch alias such as opus matching any model of its family."""
    if not reported or reported["at"] < agent.started_at:
        return agent
    model, effort = reported["model"], reported["effort"] or agent.effort
    if agent.model in (model, _family(model)) and effort == agent.effort:
        return agent
    return replace(agent, model=model, effort=effort, model_source=SESSION, model_confidence=None)
