"""Each swarm timer pass times one ledger read and one write. Two slow or failed passes in a row raise the ledger slow
alert with its deciding samples; latency recovery needs two passes below the lower threshold."""

import http.client
import json
import sys
import time
import urllib.error
from collections.abc import Callable
from dataclasses import asdict, dataclass

from scripts.inbox.seats import seat_address
from scripts.swarm import control_notifications, incidents, ledger_host, notice_text, time_left
from scripts.swarm.ledger_client import LedgerGone, LedgerRefused
from scripts.swarm.store import MASTER, SwarmError

SLOW_S = 5.0
CLEAR_S = 4.0
PASSES = 2
SENDER = "swarm"
TEXT_KEPT = 700
TIMED_OUT, NO_ANSWER, SERVER_ERROR = "timed out", "gave no answer", "answered with a server error"
REFUSED_5XX = "server refused: 5"
FAILED = (OSError, ValueError, SwarmError, SystemExit, http.client.HTTPException)
RAISED = (
    "The ledger server is slow: two swarm passes in a row took {took}. Server {cpu}, {started}. Newest on dev{newest}."
)
PAUSED = " Idle and stale claim checks and nudges pause until recovery below four seconds."
CLEARED = "The ledger server answers fast again: two swarm passes took {took}. Idle and stale claim checks resume."


@dataclass(frozen=True)
class Sample:
    read_s: float
    write_s: float
    failure: str = ""
    read_failure: str = ""
    write_failure: str = ""
    cpu: float | None = None
    cpu_before: dict | None = None
    cpu_after: dict | None = None
    wall_s: float = 0.0
    read_refusal: str = ""
    write_refusal: str = ""

    @property
    def slow(self) -> bool:
        return bool(self.failure) or max(self.read_s, self.write_s) > SLOW_S


def _failure(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return SERVER_ERROR if exc.code >= 500 else ""
    if isinstance(exc, (LedgerRefused, LedgerGone)):
        return ""
    if REFUSED_5XX in str(exc):
        return SERVER_ERROR
    return TIMED_OUT if isinstance(exc, TimeoutError) or TIMED_OUT in str(exc) else NO_ANSWER


def _timed(call: Callable[[], object], clock: Callable[[], float]) -> tuple[float, str, str]:
    started = clock()
    try:
        call()
    except FAILED as exc:
        failure = _failure(exc)
        return clock() - started, failure, "" if failure else "request refused"
    return clock() - started, "", ""


def measure(ledger, slug: str, inputs: dict, clock: Callable[[], float] = time.monotonic) -> Sample | None:
    read, write = getattr(ledger, "metadata", None), getattr(ledger, "time_left", None)
    if read is None or write is None:
        return None
    started, before = clock(), ledger_host.snapshot()
    read_s, read_failure, read_refusal = _timed(lambda: read(slug), clock)
    write_s, write_failure, write_refusal = _timed(
        lambda: write(slug, inputs.get("slots"), inputs.get("ci_minutes")), clock
    )
    after, elapsed = ledger_host.snapshot(), clock() - started
    return Sample(
        read_s=read_s,
        write_s=write_s,
        failure=read_failure or write_failure,
        read_failure=read_failure,
        write_failure=write_failure,
        cpu=ledger_host.cpu_over_pass(before, after, elapsed),
        cpu_before=before,
        cpu_after=after,
        wall_s=elapsed,
        read_refusal=read_refusal,
        write_refusal=write_refusal,
    )


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


def _passes(samples: list[dict]) -> str:
    return ". ".join(
        f"{row['slug']} pass {row['sequence']}: read {row['read_s']:.1f} seconds"
        + (f", {row['read_failure']}" if row["read_failure"] else "")
        + (f", {row['read_refusal']}" if row.get("read_refusal") else "")
        + f" and write {row['write_s']:.1f} seconds"
        + (f", {row['write_failure']}" if row["write_failure"] else "")
        + (f", {row['write_refusal']}" if row.get("write_refusal") else "")
        + (", restart failed" if row.get("watchdog") else "")
        + (f", CPU {row['cpu']:.0f} percent over pass" if row.get("cpu") is not None else ", CPU unknown over pass")
        + f", streaks before/after slow {row['streak_before']['slow']}/{row['streak_after']['slow']}"
        + f", fast {row['streak_before']['fast']}/{row['streak_after']['fast']}"
        for row in samples
    )


def _raised(samples: list[dict], facts: dict) -> str:
    started = (
        f"started {facts['started_minutes']} minutes ago"
        if facts.get("started_minutes") is not None
        else "start time unknown"
    )
    newest = f" merged {facts['merged_minutes']} minutes ago: {facts['newest']}" if facts.get("newest") else " unknown"
    label = (
        "The ledger probe failed"
        if any(row["failure"] or row.get("watchdog") for row in samples)
        else "The ledger server is slow"
    )
    context = next((row["watchdog"] for row in samples if row.get("watchdog")), "")
    return f"{label}: {_passes(samples)}. Server {started}. Newest on dev{newest}. {context}".rstrip()


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


def _incident(store, slug: str, active: bool, raised: str, resolved: str) -> dict:
    redis = store.redis
    live = {source for source in store.slugs() if store.config(source).state != "stopped"}
    root = incidents.key("ledger")

    def change(pipe):
        held = pipe.hgetall(root)
        probes = {source: value for source, value in json.loads(held.get("probes", "{}")).items() if source in live}
        probes[slug] = active and slug in live
        combined = any(probes.values()) or (held.get("active") == "1" and not resolved)
        changed = combined != (held.get("active") == "1")
        updates = {"probes": json.dumps(probes), "active": int(combined)}
        if changed:
            field = "raised_text" if combined else "resolved_text"
            updates[field] = (raised if combined else resolved) or "Ledger incident changed; triggering passes unknown."
            if combined:
                updates["generation"] = int(held.get("generation", 0)) + 1
        pipe.multi()
        pipe.hset(root, mapping=updates)
        if changed:
            pipe.rpush(f"{root}:outbox", json.dumps({"text": updates[field]}))
        return {**held, **updates}

    return redis.transaction(change, root, value_from_callable=True)


def _record(held: dict, slug: str, now_ms: int, sample: Sample, down: str) -> None:
    bad = sample.slow or bool(down)
    counts = held if held.get("samples") else {}
    before = {field: counts.get(field, 0) for field in ("slow", "fast")}
    slow = before["slow"] + 1 if bad else 0
    fast = before["fast"] + 1 if not bad and max(sample.read_s, sample.write_s) < CLEAR_S else 0
    sequence = held.get("sequence", 0) + 1
    row = {
        **asdict(sample),
        "slug": slug,
        "sequence": sequence,
        "at": now_ms,
        "watchdog": down or "",
        "raise_s": SLOW_S,
        "clear_s": CLEAR_S,
        "streak_before": before,
        "streak_after": {"slow": slow, "fast": fast},
    }
    previous = held["samples"] if held.get("bad") == bad else []
    held.update(slow=slow, fast=fast, bad=bad, sequence=sequence, samples=[*previous[-1:], row])


def _cleared(held: dict) -> str:
    needed = 1 if held.get("alert") and held.get("failure_only") else PASSES
    if held["fast"] < needed:
        return ""
    text = CLEARED.format(took=_passes(held["samples"][-needed:]))
    return text.replace("two swarm passes", "one answered fast pass") if needed == 1 else text


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
    down = store.redis.hget(incidents.key("ledger"), "watchdog")
    held = state(store, slug)
    _record(held, slug, now_ms, sample, down)
    if held.get("alert") and max(sample.read_s, sample.write_s) > SLOW_S:
        held["failure_only"] = False
    actions, notices = [], held.get("notices", [])
    raised, resolved = "", _cleared(held)
    if not held.get("alert") and held["slow"] >= PASSES:
        raised = _raised(held["samples"], (facts or ledger_host.facts)())
        failure_only = all(max(row["read_s"], row["write_s"]) <= SLOW_S for row in held["samples"])
        held.update(alert=True, raised_at=now_ms, raised_samples=held["samples"], failure_only=failure_only)
        notices = [*notices, for_operator(raised)]
        actions.append("raised the ledger slow alert")
    elif held.get("alert") and resolved:
        samples = held["samples"][-held["fast"] :]
        held.update(alert=False, cleared_samples=samples)
        notices = [*notices, for_operator(resolved)]
        actions.append("cleared the ledger slow alert")
    incident = _incident(store, slug, bool(held.get("alert")), raised, resolved)
    active = str(incident["active"]) == "1"
    text = incident.get("raised_text", "The ledger is slow or unavailable; triggering passes unknown.") + PAUSED
    if not active:
        text = incident.get("resolved_text", "The ledger incident is clear; triggering passes unknown.")
    incidents.mail(store.redis, "ledger", master_address(store, slug), text, not active)
    incidents.deliver(store.redis, "ledger", text, text)
    held["notices"] = _undelivered(ledger, slug, notices)
    store.redis.set(_key(store, slug), json.dumps(held))
    return actions
