"""The fleet session registry: one record per native session, scoped by the backend, host and process namespace that
observed it. A PID judges only records of its own scope; a seat comes only from a validated launch grant."""

import hashlib
import json
import time
from collections.abc import Callable, Collection, Mapping
from dataclasses import asdict, dataclass, replace

from hooks.proc import Process
from scripts.inbox.seats import seat_address
from scripts.swarm.naming import lane_of
from scripts.swarm.store import MASTER, RedisStore, SwarmError
from scripts.swarm_v2.auth_context import Registration
from scripts.swarm_v2.runtime.base import LOCAL, RuntimeRouter

LIVE, SUSPECT, EXITED, CLOSED = "live", "suspect", "exited", "closed"
STALE_AFTER_MS = 90_000
WRITE_ATTEMPTS = 5


@dataclass(frozen=True)
class Scope:
    backend: str
    host: str
    namespace: str


@dataclass(frozen=True)
class Session:
    session_id: str
    scope: Scope
    pid: int
    pid_start: int
    harness: str
    name: str = ""
    conversation_id: str = ""
    execution_id: str = ""
    generation: int = 0
    seat: str = ""
    state: str = LIVE
    heartbeat_ms: int = 0

    def key(self) -> str:
        return session_key(self.scope, self.session_id)

    def identity(self) -> "Session":
        return replace(self, state=LIVE, heartbeat_ms=0)


def session_key(scope: Scope, session_id: str) -> str:
    return hashlib.sha256(json.dumps([asdict(scope), session_id]).encode()).hexdigest()[:32]


def encode(record: Session) -> str:
    return json.dumps(asdict(record))


def decode(raw: str) -> Session:
    fields = json.loads(raw)
    return Session(**{**fields, "scope": Scope(**fields["scope"])})


def _now_ms() -> int:
    return int(time.time() * 1000)


class FleetRegistry:
    """The authorization callback must validate a scoped launch grant on every call; names confer no seat."""

    def __init__(
        self,
        store: RedisStore,
        slug: str,
        authorize: Callable[[str], Registration],
        clock: Callable[[], int] = _now_ms,
        stale_after_ms: int = STALE_AFTER_MS,
    ) -> None:
        self.store, self.slug, self.authorize = store, slug, authorize
        self.clock, self.stale_after_ms = clock, stale_after_ms
        self.sessions = store.key(slug, "fleet-sessions")
        self.seats = store.key(slug, "fleet-seats")

    def _grant(self, token: str, execution_id: str, generation: int) -> Registration:
        grant = self.authorize(token) if token else None
        if not isinstance(grant, Registration) or grant.swarm_id != self.slug:
            raise SwarmError("forbidden_scope")
        if (grant.execution_id, grant.generation) != (execution_id, generation):
            raise SwarmError("forbidden_scope")
        return grant

    def _seat(self, session: Session, name: str, token: str) -> str:
        seat = self._grant(token, session.execution_id, session.generation).seat_id if token else ""
        if session.seat and session.seat != seat:
            raise SwarmError("forbidden_scope")
        if lane_of(name) == MASTER and seat != seat_address(self.slug, MASTER):
            raise SwarmError("forbidden_scope")
        return seat

    @staticmethod
    def _replaceable(existing: Session, record: Session, granted: bool) -> bool:
        """False for a replay; raises when the stored record may not be replaced by this registration."""
        if existing.identity() == record.identity():
            if existing.state in (EXITED, CLOSED):
                raise SwarmError("session_ended")
            return False
        if existing.generation > record.generation:
            raise SwarmError("stale_generation")
        if existing.generation == record.generation and (existing.state == LIVE or existing.seat != record.seat):
            raise SwarmError("registration_conflict")
        if not granted:
            raise SwarmError("forbidden_scope")
        return True

    def register(self, session: Session, token: str = "") -> Session:
        name = self.store.names.resolve(session.name) if session.name else ""
        seat = self._seat(session, name, token)
        record = replace(session, name=name, seat=seat, state=LIVE, heartbeat_ms=self.clock())
        key = record.key()
        from redis.exceptions import WatchError

        for _ in range(WRITE_ATTEMPTS):
            with self.store.redis.pipeline() as pipe:
                try:
                    pipe.watch(self.sessions, self.seats)
                    raw = pipe.hget(self.sessions, key)
                    if raw and not self._replaceable(decode(raw), record, bool(token)):
                        return decode(raw)
                    held = json.loads(pipe.hget(self.seats, seat) or "null") if seat else None
                    if held and (held["generation"], held["execution_id"]) != (record.generation, record.execution_id):
                        if held["generation"] >= record.generation:
                            raise SwarmError("stale_generation")
                    pipe.multi()
                    pipe.hset(self.sessions, key, encode(record))
                    if seat:
                        owner = {"execution_id": record.execution_id, "generation": record.generation, "session": key}
                        pipe.hset(self.seats, seat, json.dumps(owner))
                    pipe.execute()
                    return record
                except WatchError:
                    continue
        raise SwarmError("dependency_unavailable")

    def _update(
        self, change: Callable[[Session], Session | None], keys: Collection[str] | None = None
    ) -> list[Session]:
        from redis.exceptions import WatchError

        for _ in range(WRITE_ATTEMPTS):
            with self.store.redis.pipeline() as pipe:
                try:
                    pipe.watch(self.sessions)
                    rows = pipe.hgetall(self.sessions)
                    changed = {}
                    for key, raw in rows.items():
                        if keys is not None and key not in keys:
                            continue
                        updated = change(decode(raw))
                        if updated is not None:
                            changed[key] = updated
                    if changed:
                        pipe.multi()
                        pipe.hset(self.sessions, mapping={key: encode(record) for key, record in changed.items()})
                        pipe.execute()
                    return list(changed.values())
                except WatchError:
                    continue
        raise SwarmError("dependency_unavailable")

    def _one(self, scope: Scope, session_id: str, token: str, change: Callable[[Session], Session]) -> Session:
        key = session_key(scope, session_id)
        raw = self.store.redis.hget(self.sessions, key)
        if not raw:
            raise SwarmError("unknown_session")
        found = decode(raw)
        if found.execution_id:
            self._grant(token, found.execution_id, found.generation)

        def owned(record: Session) -> Session:
            if (record.execution_id, record.generation) != (found.execution_id, found.generation):
                raise SwarmError("stale_generation")
            return change(record)

        return self._update(owned, {key})[0]

    def heartbeat(self, scope: Scope, session_id: str, token: str = "") -> Session:
        def beat(record: Session) -> Session:
            if record.state not in (LIVE, SUSPECT):
                raise SwarmError("session_ended")
            return replace(record, state=LIVE, heartbeat_ms=self.clock())

        return self._one(scope, session_id, token, beat)

    def close(self, scope: Scope, session_id: str, token: str = "") -> Session:
        return self._one(scope, session_id, token, lambda record: replace(record, state=CLOSED))

    def observe(self, scope: Scope, table: Mapping[int, Process]) -> int:
        """Exit this scope's sessions whose process is gone or whose PID now names another start; other scopes'
        records are never judged by this process table, and an empty table (an unreadable /proc) judges nothing."""
        if not table:
            return 0

        def judge(record: Session) -> Session | None:
            if record.scope != scope or record.state not in (LIVE, SUSPECT):
                return None
            found = table.get(record.pid)
            if found is not None and found.start_time == record.pid_start:
                return None
            return replace(record, state=EXITED)

        return len(self._update(judge))

    def sweep(self) -> int:
        deadline = self.clock() - self.stale_after_ms

        def stale(record: Session) -> Session | None:
            return replace(record, state=SUSPECT) if record.state == LIVE and record.heartbeat_ms < deadline else None

        return len(self._update(stale))

    def records(self, backends: Collection[str] | None = None) -> list[Session]:
        found = [decode(raw) for raw in self.store.redis.hvals(self.sessions)]
        return [record for record in found if backends is None or record.scope.backend in backends]

    def visible(self, environ: Mapping[str, str]) -> list[Session]:
        """Local records only while distributed launches are disabled; remote records stay stored untouched."""
        local = RuntimeRouter.from_environ((), environ).spawn_backend() == LOCAL
        return self.records(backends={LOCAL} if local else None)

    def find(self, name: str) -> list[Session]:
        canonical = self.store.names.resolve(name)
        return [record for record in self.records() if record.name == canonical]

    def seat(self, seat: str) -> dict | None:
        raw = self.store.redis.hget(self.seats, seat)
        return json.loads(raw) if raw else None

    def fleet_registry_stale_records(self) -> int:
        return sum(record.state == SUSPECT for record in self.records())
