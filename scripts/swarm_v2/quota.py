"""Fleet quota observations: one newest provider-window reading per account and harness, published under a launch
grant so the account and source execution come from the grant. A stale report never replaces a newer one, and a
missing, failed or aged reading is unknown, never full capacity. Infrastructure and operator budgets stay out."""

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from scripts import session_bands
from scripts.claude_quota_balancer import ProbeResult, QuotaWindow
from scripts.swarm import lease
from scripts.swarm.keyspace import ROOT
from scripts.swarm.store import RedisStore, SwarmError
from scripts.swarm_v2.auth_context import Registration

if TYPE_CHECKING:
    from redis import Redis

FLAG = "AGENTIHOOKS_FLEET_QUOTA"
HARNESSES = frozenset({"claude", "codex"})
STATUSES = frozenset({"allowed", "allowed_warning", "rejected", "error"})
REPORT_FIELDS = ("observed_ms", "provider_status", "five_used", "week_used", "five_reset", "week_reset")
REQUIRED = frozenset({"observed_ms", "provider_status"})
OBSERVED, UNKNOWN = "observed", "unknown"
ADMIT, WAIT, HANDOFF = "admit", "wait", "handoff"
FRESH_SECONDS = session_bands.FRESH_SECONDS
MIN_ROUTING_LEFT = 5.0
SKEW_MS = 60_000
WAIT_MS = 5 * 60 * 1000
HISTORY = 50
WRITE_ATTEMPTS = 5
STALE = f"{ROOT}:quota-stale"


@dataclass(frozen=True)
class Observation:
    account: str
    harness: str
    execution_id: str
    generation: int
    observed_ms: int
    provider_status: str
    five_used: float | None = None
    week_used: float | None = None
    five_reset: float | None = None
    week_reset: float | None = None

    def probe(self, known: bool = True) -> ProbeResult:
        failed = not known or self.provider_status == "error"
        five = QuotaWindow() if failed else QuotaWindow(self.five_used, self.five_reset)
        week = QuotaWindow() if failed else QuotaWindow(self.week_used, self.week_reset)
        return ProbeResult(self.account, self.provider_status, "FLEET", None, five, week)


@dataclass(frozen=True)
class Reading:
    account: str
    harness: str
    state: str
    age_seconds: float | None = None
    routing_left: float | None = None
    five_left: float | None = None
    week_left: float | None = None


@dataclass(frozen=True)
class Admission:
    action: str
    account: str
    until_ms: int = 0
    reason: str = ""


def encode(observation: Observation) -> str:
    return json.dumps(asdict(observation))


def decode(raw: str) -> Observation:
    return Observation(**json.loads(raw))


def latest_key(harness: str) -> str:
    return f"{ROOT}:quota:{harness}"


def history_key(account: str, harness: str) -> str:
    return f"{ROOT}:quota-history:{harness}:{account}"


def wait_key(account: str, harness: str) -> str:
    return f"{ROOT}:quota-waits:{harness}:{account}"


def latest_all(redis: "Redis", harness: str) -> dict[str, Observation]:
    """Entries that no longer decode are skipped so one drifted record cannot hide every other account."""
    found = {}
    for account, raw in redis.hgetall(latest_key(harness)).items():
        try:
            found[account] = decode(raw)
        except (ValueError, TypeError):
            continue
    return found


def _number(value: object, low: float, high: float) -> bool:
    return value is None or (type(value) in (int, float) and low <= value <= high)


def _report(report: object, now: int) -> dict:
    if not isinstance(report, dict) or not REQUIRED <= set(report) <= set(REPORT_FIELDS):
        raise SwarmError("invalid_request")
    observed = report["observed_ms"]
    if type(observed) is not int or not 0 < observed <= now + SKEW_MS:
        raise SwarmError("invalid_request")
    if not isinstance(report["provider_status"], str) or report["provider_status"] not in STATUSES:
        raise SwarmError("invalid_request")
    used = (report.get("five_used"), report.get("week_used"))
    resets = (report.get("five_reset"), report.get("week_reset"))
    if not all(_number(value, 0, 100) for value in used) or not all(
        _number(value, 1, float("inf")) for value in resets
    ):
        raise SwarmError("invalid_request")
    return {field: report.get(field) for field in REPORT_FIELDS}


class QuotaObservations:
    """The authorization callback must validate a scoped launch grant on every publish; the account comes from it."""

    def __init__(
        self,
        store: RedisStore,
        slug: str,
        authorize: Callable[[str], Registration],
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.store, self.slug, self.authorize = store, slug, authorize
        self.clock = clock or (lambda: lease.now_ms(store))

    def _grant(self, token: str) -> Registration:
        grant = self.authorize(token) if token else None
        if not isinstance(grant, Registration) or grant.swarm_id != self.slug:
            raise SwarmError("forbidden_scope")
        return grant

    def _transact(self, key: str, decide: Callable) -> object:
        from redis.exceptions import WatchError

        for _ in range(WRITE_ATTEMPTS):
            with self.store.redis.pipeline() as pipe:
                try:
                    pipe.watch(key)
                    writes, result = decide(pipe)
                    if writes:
                        pipe.multi()
                        writes(pipe)
                        pipe.execute()
                    return result
                except WatchError:
                    continue
        raise SwarmError("dependency_unavailable")

    def publish(self, token: str, harness: str, report: object) -> Observation:
        """The newest observation stored for the grant's account after this report."""
        if harness not in HARNESSES:
            raise SwarmError("invalid_request")
        fields = _report(report, self.clock())
        grant = self._grant(token)
        observation = Observation(grant.account, harness, grant.execution_id, grant.generation, **fields)
        key = latest_key(harness)

        def decide(pipe):
            raw = pipe.hget(key, grant.account)
            stored = decode(raw) if raw else None
            if stored and stored.observed_ms >= observation.observed_ms:
                return None, stored

            def writes(pipe):
                pipe.hset(key, grant.account, encode(observation))
                pipe.lpush(history_key(grant.account, harness), encode(observation))
                pipe.ltrim(history_key(grant.account, harness), 0, HISTORY - 1)

            return writes, observation

        kept = self._transact(key, decide)
        if kept != observation:
            self.store.redis.hincrby(STALE, f"{harness}:{grant.account}")
        return kept

    def latest(self, account: str, harness: str) -> Observation | None:
        raw = self.store.redis.hget(latest_key(harness), account)
        return decode(raw) if raw else None

    def history(self, account: str, harness: str) -> list[Observation]:
        return [decode(raw) for raw in self.store.redis.lrange(history_key(account, harness), 0, -1)]

    def stale_reports(self, account: str, harness: str) -> int:
        return int(self.store.redis.hget(STALE, f"{harness}:{account}") or 0)

    def quota_observation_age_seconds(self, account: str, harness: str) -> float | None:
        observation = self.latest(account, harness)
        return None if observation is None else (self.clock() - observation.observed_ms) / 1000

    def reading(self, account: str, harness: str) -> Reading:
        observation = self.latest(account, harness)
        if observation is None:
            return Reading(account, harness, UNKNOWN)
        now = self.clock()
        age = (now - observation.observed_ms) / 1000
        five = session_bands.left(observation.five_used, observation.five_reset, now / 1000)
        week = session_bands.left(observation.week_used, observation.week_reset, now / 1000)
        if observation.provider_status == "error" or five is None or week is None or age > FRESH_SECONDS:
            return Reading(account, harness, UNKNOWN, age)
        room = 0.0 if observation.provider_status == "rejected" else min(five, week)
        return Reading(account, harness, OBSERVED, age, room, five, week)

    def cap(self, account: str, harness: str) -> int:
        reading = self.reading(account, harness)
        if reading.state == UNKNOWN:
            return 0
        return session_bands.cap(reading.five_left, reading.week_left) if reading.routing_left else 0

    def _room(self, account: str, harness: str) -> bool:
        reading = self.reading(account, harness)
        return reading.state == OBSERVED and reading.routing_left >= MIN_ROUTING_LEFT

    def admit(self, account: str, harness: str, handoff: str = "") -> Admission:
        if self._room(account, harness):
            return Admission(ADMIT, account)
        reason = "unknown" if self.reading(account, harness).state == UNKNOWN else "exhausted"
        if handoff and self._room(handoff, harness):
            return Admission(HANDOFF, handoff, 0, reason)
        key, now = wait_key(account, harness), self.clock()

        def decide(pipe):
            until = int(pipe.get(key) or 0)
            if until > now:
                return None, until
            until = now + WAIT_MS
            return (lambda pipe: pipe.set(key, until)), until

        return Admission(WAIT, account, self._transact(key, decide), reason)


def fleet_observations(environ: Mapping[str, str], harness: str = "claude") -> list[tuple[float, ProbeResult]]:
    """The fleet's newest observations as router cache entries; empty when the path is off or Redis is down."""
    if environ.get(FLAG) != "1":
        return []
    from redis.exceptions import RedisError

    from scripts.swarm.store import redis_client

    try:
        found = latest_all(redis_client(environ), harness)
    except RedisError:
        return []
    now = time.time()
    return [
        (observed.observed_ms / 1000, observed.probe(session_bands.fresh(observed.observed_ms / 1000, now)))
        for observed in found.values()
    ]
