"""Fleet-wide provider-account capacity: a launch reserves a slot with an expiry, a confirmed registry record turns it
into occupancy, and only the holder's own execution and generation releases it. Local process counts stay supporting
evidence; this store is the authority for distributed launches."""

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace

from scripts.swarm import lease
from scripts.swarm.keyspace import ROOT
from scripts.swarm.store import RedisStore, SwarmError
from scripts.swarm_v2.auth_context import MAX_TTL_SECONDS, Registration
from scripts.swarm_v2.registry import CLOSED, LIVE, FleetRegistry, Scope, session_key
from scripts.swarm_v2.registry import decode as decode_session

RESERVED, OCCUPIED, ENDED = "reserved", "occupied", "ended"
WRITE_ATTEMPTS = 5
MAX_RESERVATION_MS = MAX_TTL_SECONDS * 1000
FROZEN = f"{ROOT}:accounts-frozen"
CONFLICTS = f"{ROOT}:account-reservation-conflicts"


@dataclass(frozen=True)
class Slot:
    account: str
    holder: str
    execution_id: str
    generation: int
    state: str
    expires_ms: int = 0
    session: str = ""

    def counts(self, now: int) -> bool:
        return self.state == OCCUPIED or self.expires_ms > now


def account_key(account: str) -> str:
    return f"{ROOT}:accounts:{account}"


def encode(slot: Slot) -> str:
    return json.dumps(asdict(slot))


def decode(raw: str) -> Slot:
    return Slot(**json.loads(raw))


class AccountCapacity:
    """The authorization callback must validate a scoped launch grant on every call; the account comes from it."""

    def __init__(
        self,
        store: RedisStore,
        slug: str,
        authorize: Callable[[str], Registration],
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.store, self.slug, self.authorize = store, slug, authorize
        self.clock = clock or (lambda: lease.now_ms(store))
        self.sessions = store.key(slug, "fleet-sessions")

    def _grant(self, token: str) -> Registration:
        grant = self.authorize(token) if token else None
        if not isinstance(grant, Registration) or grant.swarm_id != self.slug:
            raise SwarmError("forbidden_scope")
        return grant

    def _holder(self, seat: str) -> str:
        return f"{self.slug}/{seat}"

    @staticmethod
    def _owns(slot: Slot, grant: Registration) -> bool:
        return (slot.execution_id, slot.generation) == (grant.execution_id, grant.generation)

    def _sessions_of(self, slot: Slot) -> str:
        return self.store.key(slot.holder.split("/", 1)[0], "fleet-sessions")

    def _judge(self, reader, slots: dict[str, Slot]) -> dict[str, Slot]:
        """An occupancy whose registry record is closed or gone no longer counts."""
        judged = {}
        for holder, slot in slots.items():
            if slot.state == OCCUPIED:
                raw = reader.hget(self._sessions_of(slot), slot.session)
                if not raw or decode_session(raw).state == CLOSED:
                    slot = replace(slot, state=ENDED)
            judged[holder] = slot
        return judged

    def _write(
        self, account: str, decide: Callable[..., tuple[list[Slot], list[str], object]], *watched: str
    ) -> object:
        from redis.exceptions import WatchError

        key = account_key(account)
        for _ in range(WRITE_ATTEMPTS):
            with self.store.redis.pipeline() as pipe:
                try:
                    pipe.watch(key, FROZEN, *watched)
                    slots = {holder: decode(raw) for holder, raw in pipe.hgetall(key).items()}
                    pipe.watch(*{self._sessions_of(slot) for slot in slots.values() if slot.state == OCCUPIED}, key)
                    slots = self._judge(pipe, slots)
                    written, dropped, result = decide(pipe, slots, self.clock())
                    if written or dropped:
                        pipe.multi()
                        if dropped:
                            pipe.hdel(key, *dropped)
                        for slot in written:
                            pipe.hset(key, slot.holder, encode(slot))
                        pipe.execute()
                    return result
                except WatchError:
                    continue
        raise SwarmError("dependency_unavailable")

    def reserve(self, token: str, cap: int, ttl_ms: int) -> Slot:
        if type(cap) is not int or cap < 0 or type(ttl_ms) is not int or not 0 < ttl_ms <= MAX_RESERVATION_MS:
            raise SwarmError("invalid_request")
        grant = self._grant(token)
        holder = self._holder(grant.seat_id)

        def decide(pipe, slots, now):
            if pipe.exists(FROZEN):
                raise SwarmError("reservations_frozen")
            expired = [name for name, slot in slots.items() if not slot.counts(now)]
            held = slots.get(holder)
            if held and self._owns(held, grant) and held.counts(now):
                return [], [], held
            if held and (held.generation, held.execution_id) != (grant.generation, grant.execution_id):
                if held.generation >= grant.generation:
                    raise SwarmError("stale_generation")
            used = sum(name != holder and slot.counts(now) for name, slot in slots.items())
            if used >= cap:
                return [], [], None
            slot = Slot(grant.account, holder, grant.execution_id, grant.generation, RESERVED, now + ttl_ms)
            return [slot], expired, slot

        slot = self._write(grant.account, decide)
        if slot is None:
            self.store.redis.hincrby(CONFLICTS, grant.account)
            raise SwarmError("account_full")
        return slot

    def occupy(self, token: str, scope: Scope, session_id: str) -> Slot:
        grant = self._grant(token)
        holder, key = self._holder(grant.seat_id), session_key(scope, session_id)

        def decide(pipe, slots, now):
            held = slots.get(holder)
            if held and not self._owns(held, grant):
                raise SwarmError("stale_generation")
            if held and held.state == OCCUPIED:
                if held.session != key:
                    raise SwarmError("registration_conflict")
                return [], [], held
            if held and held.state == ENDED:
                raise SwarmError("unregistered")
            if held is None or not held.counts(now):
                raise SwarmError("reservation_expired")
            raw = pipe.hget(self.sessions, key)
            record = decode_session(raw) if raw else None
            if record is None or record.state != LIVE or not self._owns(record, grant):
                raise SwarmError("unregistered")
            slot = replace(held, state=OCCUPIED, expires_ms=0, session=key)
            return [slot], [], slot

        return self._write(grant.account, decide, self.sessions)

    def release(self, token: str) -> Slot | None:
        grant = self._grant(token)
        holder = self._holder(grant.seat_id)

        def decide(pipe, slots, now):
            held = slots.get(holder)
            if held is None:
                return [], [], None
            if not self._owns(held, grant):
                raise SwarmError("stale_generation")
            return [], [holder], held

        return self._write(grant.account, decide)

    def slots(self, account: str) -> list[Slot]:
        now = self.clock()
        found = {slot.holder: slot for slot in map(decode, self.store.redis.hvals(account_key(account)))}
        judged = self._judge(self.store.redis, found).values()
        return sorted((slot for slot in judged if slot.counts(now)), key=lambda slot: slot.holder)

    def freeze(self) -> None:
        self.store.redis.set(FROZEN, "1")

    def thaw(self) -> None:
        self.store.redis.delete(FROZEN)

    def frozen(self) -> bool:
        return bool(self.store.redis.exists(FROZEN))

    def _confirmed(
        self, registry: FleetRegistry, registration: Callable[[str], Registration | None]
    ) -> dict[str, dict[str, Slot]]:
        confirmed: dict[str, dict[str, Slot]] = {}
        for record in registry.records():
            found = registration(record.execution_id) if record.state != CLOSED and record.execution_id else None
            if found is None or found.swarm_id != self.slug or not self._owns(record, found):
                continue
            holder = self._holder(record.seat)
            slot = Slot(found.account, holder, record.execution_id, record.generation, OCCUPIED, 0, record.key())
            confirmed.setdefault(found.account, {})[holder] = slot
        return confirmed

    def reconstruct(
        self, registry: FleetRegistry, registration: Callable[[str], Registration | None]
    ) -> dict[str, int]:
        """Rebuild this swarm's occupancy from current confirmed registry records; reservations stay to expire."""
        confirmed = self._confirmed(registry, registration)
        prefix = account_key("")
        stored = {key.removeprefix(prefix) for key in self.store.redis.scan_iter(match=account_key("*"))}
        mine = self._holder("")
        rebuilt = {}
        for account in sorted(stored | set(confirmed)):
            wanted = confirmed.get(account, {})

            def decide(pipe, slots, now):
                dropped = [
                    name
                    for name, slot in slots.items()
                    if name.startswith(mine) and slot.state != RESERVED and name not in wanted
                ]
                written = [
                    slot
                    for name, slot in wanted.items()
                    if name not in slots or slots[name].generation <= slot.generation
                ]
                return written, dropped, len(wanted)

            count = self._write(account, decide)
            if count:
                rebuilt[account] = count
        return rebuilt

    def account_reservation_conflicts(self) -> dict[str, int]:
        return {account: int(count) for account, count in self.store.redis.hgetall(CONFLICTS).items()}

    def account_reservation_conflicts_total(self) -> int:
        return sum(self.account_reservation_conflicts().values())
