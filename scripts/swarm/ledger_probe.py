"""Each swarm timer pass times one ledger read and one write. Two slow or failed passes in a row raise the ledger slow
alert to the master's inbox and the operator's notifications; two fast passes clear it."""

import http.client
import json
import sys
import time
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass

from scripts.inbox.seats import seat_address
from scripts.swarm import control_notifications, incidents, ledger_host, notice_text, time_left
from scripts.swarm.ledger_client import LedgerGone, LedgerRefused
from scripts.swarm.store import MASTER, SwarmError

SLOW_S = 5.0
PASSES = 2
SENDER = "swarm"
TEXT_KEPT = 280
TIMED_OUT, NO_ANSWER, SERVER_ERROR = "timed out", "gave no answer", "answered with a server error"
REFUSED_5XX = "server refused: 5"
FAILED = (OSError, ValueError, SwarmError, SystemExit, http.client.HTTPException)
RAISED = (
    "The ledger server is slow: two swarm passes in a row took {took}. Server {cpu}, {started}. Newest on dev{newest}."
)
PAUSED = " Idle and stale claim checks and nudges pause until two fast passes."
CLEARED = "The ledger server answers fast again: two swarm passes took {took}. Idle and stale claim checks resume."


@dataclass(frozen=True)
class Sample:
    read_s: float
    write_s: float
    failure: str = ""

    @property
    def slow(self) -> bool:
        return bool(self.failure) or max(self.read_s, self.write_s) > SLOW_S

    def took(self) -> str:
        took = f"read {self.read_s:.1f} seconds and write {self.write_s:.1f} seconds"
        return f"{took}, and the ledger {self.failure}" if self.failure else took


def _failure(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return SERVER_ERROR if exc.code >= 500 else ""
    if isinstance(exc, (LedgerRefused, LedgerGone)):
        return ""
    if REFUSED_5XX in str(exc):
        return SERVER_ERROR
    return TIMED_OUT if isinstance(exc, TimeoutError) or TIMED_OUT in str(exc) else NO_ANSWER


def _timed(call: Callable[[], object], clock: Callable[[], float]) -> tuple[float, str]:
    started = clock()
    try:
        call()
    except FAILED as exc:
        return clock() - started, _failure(exc)
    return clock() - started, ""


def measure(ledger, slug: str, inputs: dict, clock: Callable[[], float] = time.monotonic) -> Sample | None:
    read, write = getattr(ledger, "metadata", None), getattr(ledger, "time_left", None)
    if read is None or write is None:
        return None
    read_s, read_failure = _timed(lambda: read(slug), clock)
    write_s, write_failure = _timed(lambda: write(slug, inputs.get("slots"), inputs.get("ci_minutes")), clock)
    return Sample(read_s, write_s, read_failure or write_failure)


def _key(store, slug: str) -> str:
    return store.key(slug, "ledger-slow")


def state(store, slug: str) -> dict:
    raw = store.redis.get(_key(store, slug))
    return json.loads(raw) if raw else {}


def holding(store, slug: str) -> bool:
    return bool(state(store, slug).get("alert"))


def master_address(store, slug: str) -> str:
    boss = control_notifications.master(store, slug)
    return (boss.seat or boss.name) if boss else seat_address(slug, MASTER)


def _raised(sample: Sample, facts: dict) -> str:
    cpu = f"CPU {facts['cpu']:.0f} percent" if facts.get("cpu") is not None else "CPU unknown"
    started = (
        f"started {facts['started_minutes']} minutes ago"
        if facts.get("started_minutes") is not None
        else "start time unknown"
    )
    newest = f" merged {facts['merged_minutes']} minutes ago: {facts['newest']}" if facts.get("newest") else " unknown"
    return RAISED.format(took=sample.took(), cpu=cpu, started=started, newest=newest)


def for_operator(text: str) -> str:
    return notice_text.plain(text, "chat")[:TEXT_KEPT]


def _delivered(ledger, slug: str, text: str) -> bool:
    try:
        ledger.notify(slug, text)
    except (LedgerRefused, LedgerGone) as exc:
        print(f"ledger slow notice dropped: {exc}", file=sys.stderr)
    except FAILED:
        return False
    return True


def _undelivered(ledger, slug: str, notices: list[str]) -> list[str]:
    for n, notice in enumerate(notices):
        if not _delivered(ledger, slug, notice):
            return notices[n:]
    return []


def observe(
    store,
    slug: str,
    ledger,
    runtime,
    now_ms: int,
    clock: Callable[[], float] = time.monotonic,
    facts: Callable[[], dict] | None = None,
) -> list[str]:
    sample = measure(ledger, slug, time_left.inputs_of(store, slug, runtime), clock)
    if sample is None:
        return []
    incidents.step(store.redis, "ledger", sample.slow)
    active = store.redis.hget(incidents.key("ledger"), "active") == "1"
    text = _raised(sample, (facts or ledger_host.facts)()) + PAUSED if active else CLEARED.format(took=sample.took())
    incidents.mail(store.redis, "ledger", master_address(store, slug), text, not active)
    incidents.deliver(store.redis, "ledger", "The ledger is slow or unavailable.", CLEARED.format(took=sample.took()))
    held = state(store, slug)
    slow = held.get("slow", 0) + 1 if sample.slow else 0
    fast = 0 if sample.slow else held.get("fast", 0) + 1
    held.update(slow=slow, fast=fast)
    actions, notices = [], held.get("notices", [])
    if not held.get("alert") and slow >= PASSES:
        text = _raised(sample, (facts or ledger_host.facts)())
        notices = [*notices, for_operator(text)]
        held.update(alert=True, raised_at=now_ms)
        actions.append("raised the ledger slow alert")
    elif held.get("alert") and fast >= PASSES:
        text = CLEARED.format(took=sample.took())
        notices = [*notices, for_operator(text)]
        held.update(alert=False)
        actions.append("cleared the ledger slow alert")
    held["notices"] = _undelivered(ledger, slug, notices)
    store.redis.set(_key(store, slug), json.dumps(held))
    return actions
