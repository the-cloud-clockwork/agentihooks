"""The stale tick watcher: a process apart from the tick, alerting every seat once per stale episode."""

import hashlib
import json
import sys
import time
from collections.abc import Callable
from threading import Lock, Thread

from scripts.inbox.store import DuplicateItem, InboxStore
from scripts.swarm import sd_notify, tick_heartbeat, timing
from scripts.swarm.store import RedisStore
from scripts.swarm_v2 import front_door

KEY = "tick-watch"
SENDER = "swarm"
PASS_S = 60
SLUG_PASS_S = 120
BEAT_S = 30
BLOCKED_LIMIT = 5
WATCHDOG = "WATCHDOG=1"
ALERT = (
    "The tick of swarm {slug} is stale: {evidence}. Until it recovers no swarm alert, nudge, merge or spawn happens; "
    "keep working your own task and do not wait on the tick. The watchdog restarts the tick, and a resolved notice "
    "follows when a fresh pass completes."
)
RESOLVED = (
    "The tick of swarm {slug} runs again: a fresh pass completed at {at}, ending the stale stretch since {since}."
)
UNWATCHED = "Swarm {slug} stopped at {at}, so its tick, stale since {since}, is no longer watched."


def recipients(store: RedisStore, slug: str) -> list[str]:
    seats = {front_door.seat(slug)}
    seats.update(agent.seat or agent.name for agent in store.agents(slug) if agent.state != "finished")
    return sorted(seats)


def watch_pass(
    store: RedisStore, slug: str, now: int, since: int, repair: Callable[[str, int], list[str]] | None = None
) -> list[str]:
    episode = _read(store, slug)
    if store.config(slug).state not in tick_heartbeat.JUDGED:
        return _resolve(store, slug, now, episode, UNWATCHED, "is no longer watched") if episode else []
    beat = tick_heartbeat.read(store, slug)
    evidence = tick_heartbeat.stale(beat, now, None if beat else since)
    if evidence:
        told = _alert(store, slug, now, episode, evidence)
        return told + (repair(slug, now) if repair and not tick_heartbeat.halted(store, slug) else [])
    return _resolve(store, slug, now, episode, RESOLVED, "runs again") if episode else []


class Repair:
    """The scheduler serves every swarm on the machine, so it restarts at most once per stale window."""

    def __init__(self, restart: Callable[[], str], respawn: Callable[[str], list[str]]):
        self.restart, self.respawn, self.restarted, self.lock = restart, respawn, None, Lock()

    def __call__(self, slug: str, now: int) -> list[str]:
        actions = []
        with self.lock:
            due = self.restarted is None or now - self.restarted >= tick_heartbeat.LIMIT_MS
            if due:
                self.restarted = now
        if due:
            actions.append(self.restart())
        return actions + self.respawn(slug)


def _alert(store, slug, now, episode, evidence) -> list[str]:
    episode = episode or {"since": now, "told": []}
    fresh = [address for address in recipients(store, slug) if address not in episode["told"]]
    text = ALERT.format(slug=slug, evidence="; ".join(evidence))
    for address in fresh:
        _send(store, address, text, _item_id(slug, episode["since"], address, "alert"))
    _save(store, slug, {**episode, "told": sorted([*episode["told"], *fresh])})
    return [f"told {address} the tick is stale" for address in fresh]


def _resolve(store, slug, now, episode, template, told) -> list[str]:
    text = template.format(slug=slug, at=tick_heartbeat.clock(now), since=tick_heartbeat.clock(episode["since"]))
    for address in episode["told"]:
        _send(store, address, text, _item_id(slug, episode["since"], address, "resolved"))
    store.redis.delete(store.key(slug, KEY))
    return [f"told {address} the tick {told}" for address in episode["told"]]


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


def run(
    store: RedisStore, now: Callable[[], int], repair=None, wait=None, passes: int | None = None, beat=None
) -> None:
    beat = beat or _watchdog
    since, done, held = now(), 0, {}
    while passes is None or done < passes:
        beat()
        for slug in store.slugs():
            _judge(store, slug, now(), since, repair, held, beat)
            beat()
        done += 1
        (wait or time.sleep)(PASS_S)


def _judge(store, slug, now, since, repair, held, beat) -> None:
    worker, blocked = held.get(slug, (None, 0))
    if worker is None or not worker.is_alive():
        worker, blocked = Thread(target=_pass, args=(store, slug, now, since, repair), daemon=True), 0
        worker.start()
    if _awaited(worker, beat):
        return
    if blocked + 1 >= BLOCKED_LIMIT:
        raise RuntimeError(f"the pass of {slug} stayed blocked through {BLOCKED_LIMIT} passes; the watcher exits")
    held[slug] = (worker, blocked + 1)
    timing.emit(sys.stderr, f"{slug}: its pass is still blocked after {SLUG_PASS_S} seconds; the watch goes on")


def _pass(store, slug, now, since, repair) -> None:
    try:
        for action in watch_pass(store, slug, now, since, repair):
            timing.emit(sys.stdout, f"{slug}: {action}")
    except Exception as exc:
        timing.emit(sys.stderr, f"{slug}: {type(exc).__name__}: {exc}")


def _awaited(worker: Thread, beat: Callable[[], None]) -> bool:
    deadline = time.monotonic() + SLUG_PASS_S
    while True:
        worker.join(min(BEAT_S, deadline - time.monotonic()))
        if not worker.is_alive() or time.monotonic() >= deadline:
            return not worker.is_alive()
        beat()


def _watchdog() -> None:
    sd_notify.notify(WATCHDOG)
