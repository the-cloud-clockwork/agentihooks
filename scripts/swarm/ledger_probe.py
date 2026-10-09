"""Each swarm timer pass times one ledger read and one write. Two slow or failed passes in a row raise the ledger slow
alert to the master's inbox and the operator's notifications; two fast passes clear it."""

import json
import time
import urllib.error
from dataclasses import dataclass

from scripts.inbox.seats import seat_address
from scripts.inbox.store import InboxStore
from scripts.swarm import control_notifications, ledger_host, notice_text
from scripts.swarm.ledger_client import LedgerGone, LedgerRefused
from scripts.swarm.store import MASTER, SwarmError

SLOW_S = 5.0
PASSES = 2
SENDER = "swarm"
ANSWERED = (LedgerRefused, LedgerGone, urllib.error.HTTPError)
RAISED = (
    "The ledger server is slow: two swarm passes in a row took {took}. Server {cpu}, {started}. Idle and stale claim "
    "checks and nudges pause until two fast passes. Newest on dev: {newest}."
)
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
        return f"{took}, failing with {self.failure}" if self.failure else took


def _timed(call, clock) -> tuple[float, str]:
    started = clock()
    try:
        call()
    except ANSWERED:
        pass
    except (OSError, SwarmError) as exc:
        return clock() - started, str(exc) or type(exc).__name__
    return clock() - started, ""


def measure(ledger, slug: str, inputs: dict, clock=time.monotonic) -> Sample | None:
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


def _master_address(store, slug: str) -> str:
    boss = control_notifications.master(store, slug)
    return (boss.seat or boss.name) if boss else seat_address(slug, MASTER)


def _raised(sample: Sample, facts: dict) -> str:
    cpu = f"CPU {facts['cpu']:.0f} percent" if facts.get("cpu") is not None else "CPU unknown"
    started = (
        f"started {facts['started_minutes']} minutes ago"
        if facts.get("started_minutes") is not None
        else "start time unknown"
    )
    newest = (
        f"{facts['newest']}, merged to dev {facts['merged_minutes']} minutes ago" if facts.get("newest") else "unknown"
    )
    return RAISED.format(took=sample.took(), cpu=cpu, started=started, newest=newest)


def _delivered(ledger, slug: str, text: str) -> bool:
    try:
        ledger.notify(slug, notice_text.plain(text, "chat"))
    except (OSError, SwarmError):
        return False
    return True


def observe(store, slug, ledger, runtime, now_ms, clock=time.monotonic, facts=None) -> list[str]:
    from scripts.swarm import time_left

    sample = measure(ledger, slug, time_left.inputs_of(store, slug, runtime), clock)
    if sample is None:
        return []
    held = state(store, slug)
    slow = held.get("slow", 0) + 1 if sample.slow else 0
    fast = 0 if sample.slow else held.get("fast", 0) + 1
    held.update(slow=slow, fast=fast)
    actions, text = [], ""
    if not held.get("alert") and slow >= PASSES:
        text = _raised(sample, (facts or ledger_host.facts)())
        held.update(alert=True, raised_at=now_ms)
        actions.append("raised the ledger slow alert")
    elif held.get("alert") and fast >= PASSES:
        text = CLEARED.format(took=sample.took())
        held.update(alert=False, cleared_at=now_ms)
        actions.append("cleared the ledger slow alert")
    if text:
        InboxStore(store.redis).send(SENDER, _master_address(store, slug), text, fyi=not held["alert"])
        held["notices"] = [*held.get("notices", []), text]
    while held.get("notices") and _delivered(ledger, slug, held["notices"][0]):
        held["notices"] = held["notices"][1:]
    store.redis.set(_key(store, slug), json.dumps(held))
    return actions
