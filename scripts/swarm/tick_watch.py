"""The stale tick watcher, a process apart from the tick that never runs a tick pass. Each stale episode sends one alert
through the Redis inbox to the front door master seat and every live agent seat with the evidence, and a resolved
notice to the same seats once a fresh pass completes. A workstation watcher also respawns an empty front door master
while the tick is stale."""

import hashlib
import json
import sys
from collections.abc import Callable

from scripts.inbox.store import DuplicateItem, InboxStore
from scripts.swarm import tick_heartbeat
from scripts.swarm.store import RedisStore
from scripts.swarm_v2 import front_door

KEY = "tick-watch"
SENDER = "swarm"
WATCHED = ("running", "paused", "stopping")
PASS_S = 60
ALERT = (
    "The tick of swarm {slug} is stale: {evidence}. Until it recovers no swarm alert, nudge, merge or spawn happens; "
    "keep working your own task and do not wait on the tick. The watchdog restarts the tick, and a resolved notice "
    "follows when a fresh pass completes."
)
RESOLVED = (
    "The tick of swarm {slug} runs again: a fresh pass completed at {at}, ending the stale stretch since {since}."
)


def recipients(store: RedisStore, slug: str) -> list[str]:
    seats = {front_door.seat(slug)}
    seats.update(agent.seat or agent.name for agent in store.agents(slug) if agent.state != "finished")
    return sorted(seats)


def watch_pass(
    store: RedisStore, slug: str, now: int, since: int, respawn: Callable[[str], list[str]] | None = None
) -> list[str]:
    episode = _read(store, slug)
    if store.config(slug).state not in WATCHED and not episode:
        return []
    evidence = tick_heartbeat.stale(tick_heartbeat.read(store, slug), now, since)
    if evidence:
        return _alert(store, slug, now, episode, evidence) + (respawn(slug) if respawn else [])
    return _resolve(store, slug, now, episode) if episode else []


def _alert(store, slug, now, episode, evidence) -> list[str]:
    episode = episode or {"since": now, "evidence": evidence, "told": []}
    fresh = [address for address in recipients(store, slug) if address not in episode["told"]]
    text = ALERT.format(slug=slug, evidence="; ".join(episode["evidence"]))
    for address in fresh:
        _send(store, address, text, _item_id(slug, episode["since"], address, "alert"))
    _save(store, slug, {**episode, "told": sorted([*episode["told"], *fresh])})
    return [f"told {address} the tick is stale" for address in fresh]


def _resolve(store, slug, now, episode) -> list[str]:
    text = RESOLVED.format(slug=slug, at=tick_heartbeat.clock(now), since=tick_heartbeat.clock(episode["since"]))
    for address in episode["told"]:
        _send(store, address, text, _item_id(slug, episode["since"], address, "resolved"))
    store.redis.delete(store.key(slug, KEY))
    return [f"told {address} the tick runs again" for address in episode["told"]]


def _send(store, address, text, item_id) -> None:
    try:
        InboxStore(store.redis).send(SENDER, address, text, fyi=True, item_id=item_id)
    except DuplicateItem:
        return


def _item_id(slug, since, address, kind) -> str:
    return hashlib.sha256(f"{KEY}:{slug}:{since}:{address}:{kind}".encode()).hexdigest()[:12]


def _read(store, slug) -> dict:
    raw = store.redis.get(store.key(slug, KEY))
    return json.loads(raw) if raw else {}


def _save(store, slug, episode) -> None:
    store.redis.set(store.key(slug, KEY), json.dumps(episode))


def run(store: RedisStore, now: Callable[[], int], respawn=None, wait=None, passes: int | None = None) -> None:
    from scripts.scheduler.process_timer import wait as scheduled_wait

    since, done = now(), 0
    while passes is None or done < passes:
        for slug in store.slugs():
            try:
                for action in watch_pass(store, slug, now(), since, respawn):
                    print(f"{slug}: {action}", flush=True)
            except Exception as exc:
                print(f"{slug}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        done += 1
        (wait or scheduled_wait)(PASS_S)
