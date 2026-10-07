"""Whether the tick may count a swarm agent idle: its herdr pane, its session heartbeat and any wait it declared.

Hooks record the heartbeat (working on prompts and tool calls, idle at Stop); `agentihooks swarm <id> wait` records
the wait. The agent is idle only when the pane reads idle, the heartbeat does not say working and no wait holds.
"""

import json

from scripts.swarm.delivery import READY
from scripts.swarm.store import PREFIX, SwarmError

WORKING, WAITING, IDLE = "working", "waiting", "idle"
STALE_MS = 20 * 60_000
BEAT_TTL_S = 24 * 3600


def key(slug, kind, name):
    return ":".join((PREFIX, slug, kind, name))


def beat(redis, slug, name, state, at):
    from scripts.swarm.naming import NameRegistry

    name = NameRegistry(redis).resolve(name)
    redis.set(key(slug, "heartbeat", name), json.dumps({"state": state, "at": at}), ex=BEAT_TTL_S)


def heartbeat(redis, slug, name):
    from scripts.swarm.naming import NameRegistry

    name = NameRegistry(redis).resolve(name)
    raw = redis.get(key(slug, "heartbeat", name))
    return json.loads(raw) if raw else None


def prompted(redis, slug, name, at):
    from scripts.swarm.naming import NameRegistry

    name = NameRegistry(redis).resolve(name)
    redis.set(key(slug, "prompt", name), at, ex=BEAT_TTL_S)


def last_prompt(redis, slug, name):
    from scripts.swarm.naming import NameRegistry

    name = NameRegistry(redis).resolve(name)
    raw = redis.get(key(slug, "prompt", name))
    return int(raw) if raw else None


def declare_wait(redis, slug, name, until, reason, at, on=None):
    from scripts.swarm.naming import NameRegistry

    name = NameRegistry(redis).resolve(name)
    if until <= at:
        raise SwarmError("a wait needs an end time in the future")
    entry = {"until": until, "reason": reason, "at": at, **({"on": on} if on else {})}
    redis.set(key(slug, "wait", name), json.dumps(entry), px=until - at)
    if named(entry):
        redis.set(key(slug, "waited", name), until, px=until - at + BEAT_TTL_S * 1000)


def named(entry):
    return bool(entry.get("on") or entry.get("reason", "").strip())


def wait(redis, slug, name):
    from scripts.swarm.naming import NameRegistry

    name = NameRegistry(redis).resolve(name)
    raw = redis.get(key(slug, "wait", name))
    return json.loads(raw) if raw else None


def waited(redis, slug, name):
    from scripts.swarm.naming import NameRegistry

    name = NameRegistry(redis).resolve(name)
    raw = redis.get(key(slug, "waited", name))
    return int(raw) if raw else 0


def end_wait(redis, slug, name, at):
    from scripts.swarm.naming import NameRegistry

    name = NameRegistry(redis).resolve(name)
    redis.delete(key(slug, "wait", name))
    redis.set(key(slug, "waited", name), at, ex=BEAT_TTL_S)


def verdict(pane, heartbeat, wait, now_ms):
    if pane == WAITING:
        return WAITING
    if pane not in READY:
        return WORKING
    if heartbeat and heartbeat.get("state") == WORKING and now_ms - heartbeat.get("at", 0) < STALE_MS:
        return WORKING
    if wait and wait.get("until", 0) > now_ms:
        return WAITING
    return IDLE


def of(store, runtime, slug, agent, now_ms):
    found = (heartbeat(store.redis, slug, agent.name), wait(store.redis, slug, agent.name))
    return verdict(runtime.status(agent), *found, now_ms)
