"""Controller transition events: a bounded sanitized audit, fixed-label latency counters and an optional exporter."""

import json
import re
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager

from scripts.swarm import lease
from scripts.swarm.store import RedisStore, SwarmError

TRANSITIONS = ("admission", "command", "heartbeat", "outcome", "fence", "recovery")
RESULTS = ("accepted", "refused", "failed")
BUCKETS = ("0.01", "0.05", "0.1", "0.5", "1", "5", "30", "+Inf")
FIELDS = (
    "task",
    "seat",
    "execution_id",
    "previous_execution_id",
    "operation_id",
    "action",
    "expected_generation",
    "observed_generation",
    "controller_epoch",
    "reason",
    "mode",
    "replacement",
    "error",
)
AUDIT_LIMIT = 2000
TIMELINE_LIMIT = 50
TEXT_LIMIT = 240
INPUT_LIMIT = 8 * TEXT_LIMIT
ENVIRONMENT_PAIRS = 3
REDACTED = "[redacted]"
WITHHELD = "[environment withheld]"
OK, DEGRADED, DISABLED = "ok", "degraded", "disabled"
AUDIT, SEQUENCE, EXPORTED, LATENCY, OBSERVABILITY = (
    "transitions",
    "transition-seq",
    "transition-exported",
    "transition-latency",
    "observability",
)
_NAMED = (
    r"[\w.-]*(?:token|secret|passw(?:or)?d|(?:api|private|access|signing|ssh)[_-]?key|credential|authorization)[\w.-]*"
)
SECRET = re.compile(
    r"(?i)\bbearer\s+[^\s'\",}]+"
    r"|\b(?:sk|ghp|gho|ghs|ghu|github_pat|xox[abprs])[-_][-\w]{8,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    rf"|({_NAMED}['\"]?\s*[=:]\s*['\"]?(?:bearer\s+)?)[^\s'\",}}]+"
)
ENVIRONMENT = re.compile(r"\b[A-Z][A-Z0-9_]+['\"]?\s*[=:]\s*\S")
USERINFO = re.compile(r"://[^/\s:@]+:[^/\s@]+@")
EXPORT_ENV = "AGENTIHOOKS_TRANSITION_EXPORT"

Exporter = Callable[[dict], None]


def _redact(match: re.Match) -> str:
    return (match.group(1) or "") + REDACTED


def sanitize_text(text: str) -> str:
    text = text[:INPUT_LIMIT]
    if "environ(" in text or len(ENVIRONMENT.findall(text)) >= ENVIRONMENT_PAIRS:
        return WITHHELD
    clean = SECRET.sub(_redact, USERINFO.sub(f"://{REDACTED}@", " ".join(text.split())))
    return clean if len(clean) <= TEXT_LIMIT else clean[: TEXT_LIMIT - 1] + "…"


def sanitize_error(error: BaseException) -> str:
    return f"{type(error).__name__}: {sanitize_text(str(error))}"


def _fields(fields: dict) -> dict:
    kept = {}
    for name in FIELDS:
        value = fields.get(name)
        if value is None or value == "":
            continue
        kept[name] = value if isinstance(value, int) and not isinstance(value, bool) else sanitize_text(str(value))
    return kept


def bucket(duration_s: float) -> str:
    return next((le for le in BUCKETS[:-1] if duration_s <= float(le)), BUCKETS[-1])


def stdout(event: dict) -> None:
    print(f"transition: {json.dumps(event, sort_keys=True)}")


def configured_exporter(environ: Mapping[str, str]) -> Exporter | None:
    return stdout if environ.get(EXPORT_ENV) == "stdout" else None


def history(events: list[dict], seat: str) -> list[dict]:
    """One seat's events in order, joined to commands through the execution ids its admissions and recoveries named."""
    ids = set()
    for event in events:
        if event.get("seat") == seat:
            ids.update(event[name] for name in ("execution_id", "replacement") if event.get(name))
    return [event for event in events if event.get("seat") == seat or event.get("execution_id") in ids]


class Telemetry:
    """Recording never raises into the caller: authority writes commit or refuse before their event is kept."""

    def __init__(
        self,
        store: RedisStore,
        slug: str,
        exporter: Exporter | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store, self.slug, self.exporter, self.clock = store, slug, exporter, clock
        self.last_error = ""

    @contextmanager
    def timed(self, transition: str, **fields) -> Iterator[dict]:
        """Set `audit` False in the yielded fields to count an accepted transition without an audit event."""
        started, seen = self.clock(), dict(fields)
        try:
            yield seen
        except SwarmError as error:
            self.record(transition, "refused", self.clock() - started, **{**seen, "error": sanitize_error(error)})
            raise
        except Exception as error:
            self.record(transition, "failed", self.clock() - started, **{**seen, "error": sanitize_error(error)})
            raise
        audit = seen.pop("audit", True)
        self.record(transition, "accepted", self.clock() - started, audit, **seen)

    def record(self, transition: str, result: str, duration_s: float, audit: bool = True, **fields) -> dict | None:
        if transition not in TRANSITIONS or result not in RESULTS:
            raise ValueError("unknown transition or result")
        duration_s = max(duration_s, 0.0)
        event = {"transition": transition, "result": result, "duration_ms": round(duration_s * 1000), **_fields(fields)}
        try:
            event = self._keep(event, duration_s, audit)
        except Exception as error:
            self.last_error = sanitize_error(error)
            return None
        if audit:
            self._export()
        return event

    def events(self, **match) -> list[dict]:
        rows = [json.loads(raw) for raw in self.store.redis.lrange(self._key(AUDIT), 0, -1)]
        return [row for row in rows if all(row.get(name) == value for name, value in match.items())]

    def execution_transition_latency_seconds(self) -> dict:
        counts = self.store.redis.hgetall(self._key(LATENCY))
        histogram = {}
        for transition in TRANSITIONS:
            for result in RESULTS:
                prefix, running, buckets = f"{transition}|{result}", 0, {}
                for le in BUCKETS:
                    running += int(counts.get(f"{prefix}|{le}", 0))
                    buckets[le] = running
                sum_s = int(counts.get(f"{prefix}|sum_ms", 0)) / 1000
                histogram.setdefault(transition, {})[result] = {"buckets": buckets, "count": running, "sum": sum_s}
        return histogram

    def _keep(self, event: dict, duration_s: float, audit: bool) -> dict:
        redis = self.store.redis
        event = {**event, "at_ms": lease.now_ms(self.store)}
        if audit:
            event["seq"] = int(redis.incr(self._key(SEQUENCE)))
        prefix = f"{event['transition']}|{event['result']}"
        pipe = redis.pipeline()
        if audit:
            pipe.rpush(self._key(AUDIT), json.dumps(event))
            pipe.ltrim(self._key(AUDIT), -AUDIT_LIMIT, -1)
        pipe.hincrby(self._key(LATENCY), f"{prefix}|{bucket(duration_s)}")
        pipe.hincrby(self._key(LATENCY), f"{prefix}|sum_ms", event["duration_ms"])
        if self.exporter is None:
            pipe.hset(self._key(OBSERVABILITY), "state", DISABLED)
        pipe.execute()
        return event

    def _pending(self, done: int) -> list[dict]:
        start = done - int(self.store.redis.get(self._key(SEQUENCE)))
        rows = [json.loads(raw) for raw in self.store.redis.lrange(self._key(AUDIT), start, -1)]
        return [row for row in rows if row["seq"] > done]

    def _export(self) -> None:
        if self.exporter is None:
            return
        redis = self.store.redis
        try:
            done = int(redis.get(self._key(EXPORTED)) or 0)
            for event in self._pending(done):
                self.exporter(event)
                if gap := event["seq"] - done - 1:
                    redis.hincrby(self._key(OBSERVABILITY), "export_dropped", gap)
                done = event["seq"]
                redis.set(self._key(EXPORTED), done)
            redis.hset(self._key(OBSERVABILITY), mapping={"state": OK, "exported_at_ms": lease.now_ms(self.store)})
        except Exception as error:
            self._degrade(error)

    def _degrade(self, error: Exception) -> None:
        self.last_error = sanitize_error(error)
        try:
            key = self._key(OBSERVABILITY)
            self.store.redis.hset(
                key,
                mapping={"state": DEGRADED, "last_error": self.last_error, "failed_at_ms": lease.now_ms(self.store)},
            )
            self.store.redis.hincrby(key, "export_failures")
        except Exception as missed:
            self.last_error = sanitize_error(missed)

    def _key(self, name: str) -> str:
        return self.store.key(self.slug, name)


def observability(store: RedisStore, slug: str) -> dict:
    row = store.redis.hgetall(store.key(slug, OBSERVABILITY))
    events = int(store.redis.get(store.key(slug, SEQUENCE)) or 0)
    state = row.get("state", DISABLED)
    exported = int(store.redis.get(store.key(slug, EXPORTED)) or 0)
    return {
        "state": state,
        "events": events,
        "unexported": 0 if state == DISABLED else events - exported,
        "export_failures": int(row.get("export_failures", 0)),
        "export_dropped": int(row.get("export_dropped", 0)),
        "last_error": row.get("last_error", ""),
        "failed_at_ms": int(row.get("failed_at_ms", 0)),
        "exported_at_ms": int(row.get("exported_at_ms", 0)),
    }


def timelines(store: RedisStore, slug: str, seats: Iterable[str]) -> dict[str, list[dict]]:
    events = Telemetry(store, slug).events()
    return {seat: history(events, seat)[-TIMELINE_LIMIT:] for seat in seats if seat}
