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


def key(slug, kind, name, prefix=PREFIX):
    return ":".join((prefix, slug, kind, name))


def _resolved(redis, slug, name):
    from scripts.hive import scope
    from scripts.swarm.naming import NameRegistry

    name = NameRegistry(redis).resolve(name)
    return name, scope.readings(redis, slug, name)


def beat(redis, slug, name, state, at):
    name, prefix = _resolved(redis, slug, name)
    redis.set(key(slug, "heartbeat", name, prefix), json.dumps({"state": state, "at": at}), ex=BEAT_TTL_S)


def heartbeat(redis, slug, name):
    name, prefix = _resolved(redis, slug, name)
    raw = redis.get(key(slug, "heartbeat", name, prefix))
    return json.loads(raw) if raw else None


def prompted(redis, slug, name, at):
    name, prefix = _resolved(redis, slug, name)
    redis.set(key(slug, "prompt", name, prefix), at, ex=BEAT_TTL_S)


def last_prompt(redis, slug, name):
    name, prefix = _resolved(redis, slug, name)
    raw = redis.get(key(slug, "prompt", name, prefix))
    return int(raw) if raw else None


def begin_turn(redis, slug, name):
    name, prefix = _resolved(redis, slug, name)
    redis.incr(key(slug, "turn", name, prefix))


def turn(redis, slug, name):
    name, prefix = _resolved(redis, slug, name)
    return redis.get(key(slug, "turn", name, prefix))


def remember_wait(redis, slug: str, name: str, entry: dict, end: int, prefix: str) -> None:
    ended = {**entry, "until": min(entry["until"], end)}
    redis.hset(key(slug, "wait-history", name, prefix), str(entry["at"]), json.dumps(ended))


def wait_history(redis, slug: str, name: str) -> list[dict]:
    from scripts.swarm.naming import NameRegistry

    names = NameRegistry(redis)
    name, prefix = _resolved(redis, slug, name)
    entries = {}
    for candidate in [*names.aliases(name), name]:
        for source in dict.fromkeys((PREFIX, prefix)):
            entries.update(redis.hgetall(key(slug, "wait-history", candidate, source)))
    return sorted((json.loads(raw) for raw in entries.values()), key=lambda held: held["at"])


def declare_wait(redis, slug, name, until, reason, at, on=None):
    name, prefix = _resolved(redis, slug, name)
    if until <= at:
        raise SwarmError("a wait needs an end time in the future")
    entry = {"until": until, "reason": reason, "at": at, **({"on": on} if on else {})}
    active = key(slug, "wait", name, prefix)

    def update(pipe):
        previous = pipe.get(active)
        pipe.multi()
        if previous:
            remember_wait(pipe, slug, name, json.loads(previous), at, prefix)
        remember_wait(pipe, slug, name, entry, until, prefix)
        pipe.set(active, json.dumps(entry), px=until - at)
        if named(entry):
            pipe.set(key(slug, "waited", name, prefix), until, px=until - at + BEAT_TTL_S * 1000)

    redis.transaction(update, active)


def named(entry):
    return bool(entry.get("on") or entry.get("reason", "").strip())


def wait(redis, slug, name):
    name, prefix = _resolved(redis, slug, name)
    raw = redis.get(key(slug, "wait", name, prefix))
    return json.loads(raw) if raw else None


def waited(redis, slug, name):
    name, prefix = _resolved(redis, slug, name)
    raw = redis.get(key(slug, "waited", name, prefix))
    return int(raw) if raw else 0


def woke(redis, slug, name):
    name, prefix = _resolved(redis, slug, name)
    raw = redis.get(key(slug, "woke", name, prefix))
    return int(raw) if raw else 0


def end_wait(redis, slug, name, at):
    name, prefix = _resolved(redis, slug, name)
    active = key(slug, "wait", name, prefix)

    def update(pipe):
        previous = pipe.get(active)
        pipe.multi()
        if previous:
            remember_wait(pipe, slug, name, json.loads(previous), at, prefix)
        pipe.delete(active)
        pipe.set(key(slug, "waited", name, prefix), at, ex=BEAT_TTL_S)

    redis.transaction(update, active)


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
