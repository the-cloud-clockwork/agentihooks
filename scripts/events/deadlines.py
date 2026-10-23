import hashlib
import json
from dataclasses import replace

from scripts.events.envelope import Envelope
from scripts.events.subjects import subject as event_subject

KINDS = frozenset({"idle", "stale", "handoff"})
REQUESTS = frozenset({"deadline.scheduled", "deadline.cancelled"})
SUBJECT = "swarm.*.deadline.*"


def _digest(parts: list) -> str:
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def _kind(kind: str) -> None:
    if kind not in KINDS:
        raise ValueError(f"unknown deadline kind {kind!r}")


def _deadline(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("deadline must be a non negative integer")


def _request(source: Envelope, kind: str, deadline_ms: int | None, action: str) -> Envelope:
    _kind(kind)
    return replace(
        source,
        id=_digest([source.id, kind, action]),
        type=f"deadline.{action}",
        entity="timer",
        key=_digest([source.swarm, source.entity, source.key, kind]),
        cause=source.id,
        data={"kind": kind, "entity": source.entity, "key": source.key, "deadline_ms": deadline_ms},
    )


def schedule(source: Envelope, kind: str, deadline_ms: int) -> Envelope:
    """Versions must increase per target and kind; target keys identify one execution or claim lifetime."""
    _deadline(deadline_ms)
    return _request(source, kind, deadline_ms, "scheduled")


def cancel(source: Envelope, kind: str) -> Envelope:
    return _request(source, kind, None, "cancelled")


def validate(request: Envelope) -> None:
    if request.type not in REQUESTS or request.entity != "timer":
        raise ValueError("expected a deadline request")
    data = request.data
    if set(data) != {"kind", "entity", "key", "deadline_ms"}:
        raise ValueError("deadline request fields differ")
    _kind(data["kind"])
    if not all(isinstance(data[name], str) and data[name] for name in ("entity", "key")):
        raise ValueError("deadline target must have an entity and key")
    if request.key != _digest([request.swarm, data["entity"], data["key"], data["kind"]]):
        raise ValueError("deadline key differs from its target")
    if request.type == "deadline.scheduled":
        _deadline(data["deadline_ms"])
    elif data["deadline_ms"] is not None:
        raise ValueError("cancelled deadline must be null")


def expired(request: Envelope) -> Envelope:
    return replace(
        request,
        id=_digest([request.id, "expired"]),
        type="deadline.expired",
        time=request.data["deadline_ms"],
        author="timers",
        cause=request.id,
    )


def subject(event: Envelope) -> str:
    return event_subject(event.swarm, *event.type.split("."))
