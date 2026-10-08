"""Observation confidence and failure classification for one execution attempt.

Heartbeat freshness, the Kubernetes view, the supervisor handshake, terminal reachability and provider wait stay
separate signals, each with its source and time; a denied or unreachable read is never evidence that a Pod is gone.
"""

import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from scripts.swarm import idle
from scripts.swarm.store import AgentRecord

MODE_VARIABLE = "AGENTIHOOKS_OBSERVATION_MODE"
RUNNING, PENDING = "Running", "Pending"
ENDED = frozenset(("Succeeded", "Failed"))
HANDSHAKE, EXITED, QUOTA_WAIT = "confirmed", "exited", "quota_wait"
WRITE_ATTEMPTS = 5
UNCLASSIFIED = "unclassified"


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
    pass


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
    sources: dict
    denied: tuple = ()
    suspect_since: float = 0.0
    needs_operator: bool = False
    recovered: bool = False


def _latest(signals: Iterable[Signal]) -> dict[Source, Signal]:
    latest = {}
    for found in signals:
        if found.source not in latest or found.observed_at > latest[found.source].observed_at:
            latest[found.source] = found
    return latest


def _value(latest: dict[Source, Signal], source: Source) -> str:
    found = latest.get(source)
    return found.value if found and found.reading is Reading.OK else ""


def _terminal(found: Signal | None) -> Terminal:
    if found is None:
        return Terminal.UNOBSERVED
    return Terminal.REACHABLE if found.reading is Reading.OK else Terminal.DEGRADED


def _judge(fresh: bool, beat: bool, pod: Reading, latest: dict[Source, Signal]) -> tuple[State, Failure, Confidence]:
    """LOST here names failure proof only; classify decides whether the suspect threshold has passed."""
    phase, supervisor = _value(latest, Source.KUBERNETES), _value(latest, Source.SUPERVISOR)
    gone = pod is Reading.NOT_FOUND or phase in ENDED or supervisor == EXITED
    if fresh and gone:
        return State.SUSPECT, Failure.WORKER_LOSS, Confidence.UNCERTAIN
    if fresh:
        agree = Confidence.CONFIRMED if phase == RUNNING or supervisor == HANDSHAKE else Confidence.PARTIAL
        if _value(latest, Source.PROVIDER) == QUOTA_WAIT:
            return State.WAITING_QUOTA, Failure.PROVIDER_WAIT, agree
        return State.WORKING, Failure.NONE, agree
    if gone:
        return State.LOST, Failure.WORKER_LOSS, Confidence.PARTIAL
    if phase == PENDING:
        return State.PENDING, Failure.POD_SCHEDULING, Confidence.PARTIAL
    if pod in DENIED:
        return State.SUSPECT, Failure.OBSERVATION_DENIED, Confidence.UNCERTAIN
    if pod is not Reading.OK:
        return State.SUSPECT, Failure.OBSERVATION_UNAVAILABLE, Confidence.UNCERTAIN
    if not beat and supervisor != HANDSHAKE:
        return State.STARTING, Failure.NONE, Confidence.PARTIAL
    return State.SUSPECT, Failure.WORKER_LOSS, Confidence.UNCERTAIN


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
    latest = _latest(signals)
    beat = latest.get(Source.HEARTBEAT)
    fresh = beat is not None and beat.reading is Reading.OK and now - beat.observed_at <= thresholds.fresh_s
    pod = latest[Source.KUBERNETES].reading if Source.KUBERNETES in latest else Reading.UNREACHABLE
    state, failure, confidence = _judge(fresh, beat is not None, pod, latest)
    denied = tuple(sorted(found.source.value for found in latest.values() if found.reading in DENIED))
    terminal = _terminal(latest.get(Source.TERMINAL))
    if failure is Failure.NONE and terminal is Terminal.DEGRADED:
        failure = Failure.TERMINAL_LOSS
    elif failure is Failure.NONE and denied:
        failure = Failure.OBSERVATION_DENIED
    was_suspect = kept is not None and kept.state is State.SUSPECT
    since = 0.0
    if state in (State.SUSPECT, State.LOST):
        since = kept.suspect_since if was_suspect else now
    conservative = thresholds.mode is Mode.CONSERVATIVE
    if state is State.LOST and (conservative or not was_suspect or now - since < thresholds.lost_after_s):
        state = State.SUSPECT
    return Classification(
        execution_id,
        generation,
        state,
        terminal,
        failure,
        confidence,
        max((found.observed_at for found in latest.values()), default=0.0),
        {
            found.source.value: {"reading": found.reading.value, "observed_at": found.observed_at, "value": found.value}
            for found in latest.values()
        },
        denied,
        since,
        conservative and state is State.SUSPECT,
        was_suspect and state in (State.WORKING, State.WAITING_QUOTA),
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


def _key(store: Any, slug: str) -> str:
    return store.key(slug, "observations")


def stored(store: Any, slug: str, execution_id: str) -> Classification | None:
    return _decode(store.redis.hget(_key(store, slug), execution_id))


class Observer:
    def __init__(self, store: Any, backend: str, thresholds: Thresholds):
        self.store, self.backend, self.thresholds = store, backend, thresholds
        self.discarded = 0

    def observe(self, slug: str, agent: AgentRecord, signals: Iterable[Signal], now: float) -> Classification:
        if agent.runtime_backend != self.backend:
            raise ObservationRefused(f"runtime object belongs to {agent.runtime_backend}")
        occupant = self.store.execution_registry.occupants(slug).get(agent.seat)
        identity = (agent.execution_id, agent.generation)
        if not agent.execution_id or occupant is None or (occupant.execution_id, occupant.generation) != identity:
            raise ObservationRefused("execution is not the current occupant of its seat")
        signals = list(signals)
        own = [found for found in signals if (found.execution_id, found.generation) == identity]
        self.discarded += len(signals) - len(own)
        return self._record(slug, agent, own, now)

    def get(self, slug: str, execution_id: str) -> Classification | None:
        return stored(self.store, slug, execution_id)

    def records(self, slug: str) -> list[Classification]:
        return [_decode(raw) for raw in self.store.redis.hvals(_key(self.store, slug))]

    def execution_observation_age_seconds(self, slug: str, now: float) -> dict[str, float]:
        return {record.execution_id: now - record.observed_at for record in self.records(slug)}

    def _record(self, slug: str, agent: AgentRecord, signals: list[Signal], now: float) -> Classification:
        from redis.exceptions import WatchError

        key = _key(self.store, slug)
        for _ in range(WRITE_ATTEMPTS):
            with self.store.redis.pipeline() as pipe:
                try:
                    pipe.watch(key)
                    prior = _decode(pipe.hget(key, agent.execution_id))
                    seen = classify(agent.execution_id, agent.generation, signals, prior, self.thresholds, now)
                    if prior and (prior.generation, prior.observed_at) > (seen.generation, seen.observed_at):
                        return prior
                    pipe.multi()
                    pipe.hset(key, agent.execution_id, json.dumps(asdict(seen)))
                    pipe.execute()
                    return seen
                except WatchError:
                    continue
        raise ObservationRefused("observation record kept changing; nothing recorded")


def projection(store: Any, slug: str, agent: AgentRecord) -> dict:
    found = stored(store, slug, agent.execution_id) if agent.execution_id else None
    if found:
        return {
            "state": found.state,
            "terminal": found.terminal,
            "failure": found.failure,
            "confidence": found.confidence,
            "observed_at": found.observed_at,
            "sources": found.sources,
        }
    beat = idle.heartbeat(store.redis, slug, agent.name)
    if not beat:
        return {"state": UNCLASSIFIED, "observed_at": 0.0, "sources": {}}
    at = beat["at"] / 1000
    heartbeat = {"reading": Reading.OK, "observed_at": at, "value": beat["state"]}
    return {"state": UNCLASSIFIED, "observed_at": at, "sources": {Source.HEARTBEAT: heartbeat}}
