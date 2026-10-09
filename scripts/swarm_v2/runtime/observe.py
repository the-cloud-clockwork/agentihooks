"""A denied or unreachable read is never evidence that a Pod is gone."""

import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from scripts.swarm.store import AgentRecord
from scripts.swarm_v2.runtime.operations import WRITE_ATTEMPTS

MODE_VARIABLE = "AGENTIHOOKS_OBSERVATION_MODE"
RUNNING, PENDING = "Running", "Pending"
ENDED = frozenset(("Succeeded", "Failed"))
HANDSHAKE, EXITED, QUOTA_WAIT = "confirmed", "exited", "quota_wait"


class Source(StrEnum):
    HEARTBEAT = "heartbeat"
    KUBERNETES = "kubernetes"
    SUPERVISOR = "supervisor"
    TERMINAL = "terminal"
    PROVIDER = "provider"


class Reading(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    UNAUTHENTICATED = "unauthenticated"
    UNREACHABLE = "unreachable"


DENIED = frozenset((Reading.FORBIDDEN, Reading.UNAUTHENTICATED))


class State(StrEnum):
    PENDING = "pending"
    STARTING = "starting"
    WORKING = "working"
    WAITING_QUOTA = "waiting_quota"
    SUSPECT = "suspect"
    LOST = "lost"


ALIVE = frozenset((State.WORKING, State.WAITING_QUOTA))


class Terminal(StrEnum):
    REACHABLE = "reachable"
    DEGRADED = "degraded"
    UNOBSERVED = "unobserved"


class Failure(StrEnum):
    NONE = "none"
    TERMINAL_LOSS = "terminal_loss"
    WORKER_LOSS = "worker_loss"
    POD_SCHEDULING = "pod_scheduling"
    PROVIDER_WAIT = "provider_wait"
    OBSERVATION_DENIED = "observation_denied"
    OBSERVATION_UNAVAILABLE = "observation_unavailable"


class Confidence(StrEnum):
    CONFIRMED = "confirmed"
    PARTIAL = "partial"
    UNCERTAIN = "uncertain"


class Mode(StrEnum):
    STANDARD = "standard"
    CONSERVATIVE = "conservative"


class ObservationRefused(ValueError):
    def __init__(self, message: str, error_class: str, retryable: bool = False):
        super().__init__(message)
        self.error_class, self.retryable = error_class, retryable


@dataclass(frozen=True)
class Signal:
    source: Source
    reading: Reading
    observed_at: float
    execution_id: str
    generation: int
    value: str = ""


@dataclass(frozen=True)
class Thresholds:
    fresh_s: float = 120.0
    lost_after_s: float = 600.0
    mode: Mode = Mode.STANDARD

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> "Thresholds":
        return cls(mode=Mode(environ.get(MODE_VARIABLE) or Mode.STANDARD))


@dataclass(frozen=True)
class Classification:
    execution_id: str
    generation: int
    state: State
    terminal: Terminal
    failure: Failure
    confidence: Confidence
    observed_at: float
    confirmed_at: float
    classified_at: float
    sources: dict[str, dict[str, Any]]
    denied: tuple[str, ...] = ()
    suspect_since: float = 0.0
    proof_since: float = 0.0
    needs_operator: bool = False
    recovered: bool = False


@dataclass(frozen=True)
class Seen:
    reading: Reading
    observed_at: float
    value: str


def _remembered(prior: Classification | None) -> dict[Source, Seen]:
    if prior is None:
        return {}
    return {
        Source(name): Seen(Reading(entry["reading"]), entry["observed_at"], entry["value"])
        for name, entry in prior.sources.items()
    }


def _latest(signals: Iterable[Signal], known: dict[Source, Seen]) -> dict[Source, Seen]:
    latest = dict(known)
    for found in signals:
        held = latest.get(found.source)
        if held is None or found.observed_at >= held.observed_at:
            latest[found.source] = Seen(found.reading, found.observed_at, found.value)
    return latest


def _values(seen: dict[Source, Seen]) -> dict[Source, str]:
    return {source: entry.value for source, entry in seen.items() if entry.reading is Reading.OK}


def _terminal(found: Seen | None) -> Terminal:
    if found is None:
        return Terminal.UNOBSERVED
    return Terminal.REACHABLE if found.reading is Reading.OK else Terminal.DEGRADED


def _alive(gone: bool, values: dict[Source, str], current: dict[Source, str]) -> tuple[State, Failure, Confidence]:
    if gone:
        return State.SUSPECT, Failure.WORKER_LOSS, Confidence.UNCERTAIN
    if values.get(Source.KUBERNETES) == PENDING:
        return State.SUSPECT, Failure.POD_SCHEDULING, Confidence.UNCERTAIN
    agrees = current.get(Source.KUBERNETES) == RUNNING or current.get(Source.SUPERVISOR) == HANDSHAKE
    agree = Confidence.CONFIRMED if agrees else Confidence.PARTIAL
    if values.get(Source.PROVIDER) == QUOTA_WAIT:
        return State.WAITING_QUOTA, Failure.PROVIDER_WAIT, agree
    return State.WORKING, Failure.NONE, agree


def _judge(
    fresh: bool, beat: bool, latest: dict[Source, Seen], current: dict[Source, Seen]
) -> tuple[State, Failure, Confidence]:
    """LOST here names failure proof only; classify decides whether the proof has held long enough."""
    pod = latest[Source.KUBERNETES].reading if Source.KUBERNETES in latest else Reading.UNREACHABLE
    values = _values(latest)
    phase, supervisor = values.get(Source.KUBERNETES), values.get(Source.SUPERVISOR)
    gone = pod is Reading.NOT_FOUND or phase in ENDED or supervisor == EXITED
    if fresh:
        return _alive(gone, values, _values(current))
    if gone:
        return State.LOST, Failure.WORKER_LOSS, Confidence.PARTIAL
    if phase == PENDING:
        return State.PENDING, Failure.POD_SCHEDULING, Confidence.PARTIAL
    if pod in DENIED:
        return State.SUSPECT, Failure.OBSERVATION_DENIED, Confidence.UNCERTAIN
    if pod is not Reading.OK:
        return State.SUSPECT, Failure.OBSERVATION_UNAVAILABLE, Confidence.UNCERTAIN
    if beat:
        return State.SUSPECT, Failure.WORKER_LOSS, Confidence.UNCERTAIN
    if supervisor == HANDSHAKE:
        return State.WORKING, Failure.NONE, Confidence.PARTIAL
    return State.STARTING, Failure.NONE, Confidence.PARTIAL


def _qualified(failure: Failure, terminal: Terminal, denied: tuple[str, ...]) -> Failure:
    if failure is Failure.NONE and terminal is Terminal.DEGRADED:
        return Failure.TERMINAL_LOSS
    if failure is Failure.NONE and denied:
        return Failure.OBSERVATION_DENIED
    return failure


def _settled(state: State, kept: Classification | None, thresholds: Thresholds, now: float) -> State:
    proven_since = kept.proof_since if kept else 0.0
    held = proven_since and now - proven_since >= thresholds.lost_after_s
    if state is State.LOST and (thresholds.mode is Mode.CONSERVATIVE or not held):
        return State.SUSPECT
    return state


def classify(
    execution_id: str,
    generation: int,
    signals: Iterable[Signal],
    prior: Classification | None,
    thresholds: Thresholds,
    now: float,
) -> Classification:
    kept = prior if prior and prior.generation == generation else None
    if kept and kept.state is State.LOST:
        return kept
    latest = _latest(signals, _remembered(kept))
    beat = latest.get(Source.HEARTBEAT)
    fresh = beat is not None and beat.reading is Reading.OK and now - beat.observed_at <= thresholds.fresh_s
    current = {source: found for source, found in latest.items() if now - found.observed_at <= thresholds.fresh_s}
    state, failure, confidence = _judge(fresh, beat is not None, latest, current)
    denied = tuple(sorted(source.value for source, found in latest.items() if found.reading in DENIED))
    terminal = _terminal(latest.get(Source.TERMINAL))
    proof_since = 0.0
    if state is State.LOST:
        proof_since = kept.proof_since if kept and kept.proof_since else now
    state = _settled(state, kept, thresholds, now)
    was_suspect = kept is not None and kept.state is State.SUSPECT
    confirmed = [found.observed_at for found in latest.values() if found.reading is Reading.OK]
    return Classification(
        execution_id,
        generation,
        state,
        terminal,
        _qualified(failure, terminal, denied),
        confidence,
        max((found.observed_at for found in latest.values()), default=0.0),
        max([kept.confirmed_at if kept else 0.0, *confirmed]),
        now,
        {
            source.value: {"reading": found.reading.value, "observed_at": found.observed_at, "value": found.value}
            for source, found in latest.items()
        },
        denied,
        (kept.suspect_since if was_suspect else now) if state in (State.SUSPECT, State.LOST) else 0.0,
        proof_since,
        thresholds.mode is Mode.CONSERVATIVE and state is State.SUSPECT,
        was_suspect and state in ALIVE,
    )


def _decode(raw: str | None) -> Classification | None:
    if not raw:
        return None
    values = json.loads(raw)
    return Classification(
        **{
            **values,
            "state": State(values["state"]),
            "terminal": Terminal(values["terminal"]),
            "failure": Failure(values["failure"]),
            "confidence": Confidence(values["confidence"]),
            "denied": tuple(values["denied"]),
        }
    )


def _key(store: Any, slug: str, kind: str = "observations") -> str:
    return store.key(slug, kind)


def stored(store: Any, slug: str, execution_id: str) -> Classification | None:
    return _decode(store.redis.hget(_key(store, slug), execution_id))


class Observer:
    def __init__(self, store: Any, backend: str, thresholds: Thresholds):
        self.store, self.backend, self.thresholds = store, backend, thresholds
        self.discarded = 0

    def observe(self, slug: str, agent: AgentRecord, signals: Iterable[Signal], now: float) -> Classification:
        if agent.runtime_backend != self.backend:
            raise ObservationRefused(f"runtime object belongs to {agent.runtime_backend}", "forbidden_scope")
        occupant = self.store.execution_registry.occupants(slug).get(agent.seat)
        identity = (agent.execution_id, agent.generation)
        if not agent.execution_id or occupant is None or (occupant.execution_id, occupant.generation) != identity:
            raise ObservationRefused("execution is not the current occupant of its seat", "stale_generation")
        signals = list(signals)
        own = [found for found in signals if (found.execution_id, found.generation) == identity]
        self.discarded += len(signals) - len(own)
        return self._record(slug, agent, own, now)

    def get(self, slug: str, execution_id: str) -> Classification | None:
        return stored(self.store, slug, execution_id)

    def records(self, slug: str) -> list[Classification]:
        return [_decode(raw) for raw in self.store.redis.hvals(_key(self.store, slug))]

    def audit(self, slug: str) -> list[Classification]:
        return [_decode(raw) for raw in self.store.redis.lrange(_key(self.store, slug, "observation-audit"), 0, -1)]

    def execution_observation_age_seconds(self, slug: str, now: float) -> dict[str, float | None]:
        return {
            record.execution_id: now - record.confirmed_at if record.confirmed_at else None
            for record in self.records(slug)
        }

    def _record(self, slug: str, agent: AgentRecord, signals: list[Signal], now: float) -> Classification:
        from redis.exceptions import WatchError

        key = _key(self.store, slug)
        for _ in range(WRITE_ATTEMPTS):
            with self.store.redis.pipeline() as pipe:
                try:
                    pipe.watch(key)
                    prior = _decode(pipe.hget(key, agent.execution_id))
                    if prior and (prior.generation, prior.classified_at) > (agent.generation, now):
                        return prior
                    seen = classify(agent.execution_id, agent.generation, signals, prior, self.thresholds, now)
                    pipe.multi()
                    pipe.hset(key, agent.execution_id, json.dumps(asdict(seen)))
                    if seen.state is State.LOST and (prior is None or prior.state is not State.LOST):
                        pipe.rpush(_key(self.store, slug, "observation-audit"), json.dumps(asdict(seen)))
                    pipe.execute()
                    return seen
                except WatchError:
                    continue
        raise ObservationRefused("observation record kept changing; nothing recorded", "revision_conflict", True)
