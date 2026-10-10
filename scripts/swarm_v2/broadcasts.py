"""Fleet broadcasts: canonical messages and per-seat delivery state under the swarm's Redis keys. Routing matches fleet,
brain, project, target role and channel together, so a channel name alone never reaches another fleet or brain. The
local broadcast file only caches what a seat claimed; it is never the fleet authority."""

import json
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from scripts.swarm import lease
from scripts.swarm.store import RedisStore, SwarmError
from scripts.swarm_v2.auth_context import Registration

FLAG = "AGENTIHOOKS_FLEET_BROADCASTS"
ONCE, UNTIL_ACK = "once", "until_ack"
POLICIES = {
    "nuclear": UNTIL_ACK,
    "critical": UNTIL_ACK,
    "alert": UNTIL_ACK,
    "warning": UNTIL_ACK,
    "info": ONCE,
    "resolved": ONCE,
}
OPERATOR_ONLY = frozenset({"nuclear", "critical"})
DRAFT = frozenset(
    {"broadcast_id", "channel", "severity", "message", "ttl_seconds", "brain_id", "project_id", "target_role", "policy"}
)
REQUIRED = frozenset({"severity", "message", "ttl_seconds"})
MAX_TTL_SECONDS = 86400
MAX_MESSAGE = 8192
NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
WRITE_ATTEMPTS = 5


@dataclass(frozen=True)
class Broadcast:
    broadcast_id: str
    revision: int
    fleet: str
    brain_id: str
    project_id: str
    target_role: str
    channel: str
    severity: str
    policy: str
    message: str
    author: str
    published_ms: int
    expires_ms: int

    def content(self) -> tuple:
        return _content(asdict(self))


@dataclass(frozen=True)
class Delivery:
    broadcast: Broadcast
    delivered_ms: int
    lag_seconds: float


def encode(broadcast: Broadcast) -> str:
    return json.dumps(asdict(broadcast), sort_keys=True)


def decode(raw: str) -> Broadcast:
    return Broadcast(**json.loads(raw))


def _delivery(found: dict) -> Delivery:
    return Delivery(Broadcast(**found["broadcast"]), found["delivered_ms"], found["lag_seconds"])


def role_of(seat: str) -> str:
    return seat.split("@", 1)[0].rsplit("-", 1)[0]


def owner_of(author: str) -> str:
    """Any operator may revise an operator broadcast; an agent's broadcast belongs to its seat across generations."""
    return "operator" if author.startswith("operator:") else author.split("/", 1)[0]


def _content(fields: dict) -> tuple:
    names = ("brain_id", "project_id", "target_role", "channel", "severity", "policy", "message")
    return tuple(fields[name] for name in names)


def _name(value: object, empty: bool = True) -> str:
    if value == "" and empty:
        return ""
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise SwarmError("invalid_request")
    return value


def _draft(draft: object) -> dict:
    if not isinstance(draft, dict) or not REQUIRED <= set(draft) <= DRAFT:
        raise SwarmError("invalid_request")
    message, severity, ttl = draft["message"], draft["severity"], draft["ttl_seconds"]
    if not isinstance(message, str) or not message.strip() or len(message) > MAX_MESSAGE:
        raise SwarmError("invalid_request")
    if severity not in POLICIES or type(ttl) is not int or not 0 < ttl <= MAX_TTL_SECONDS:
        raise SwarmError("invalid_request")
    policy = draft.get("policy", POLICIES[severity])
    project = draft.get("project_id", "")
    if policy not in (ONCE, UNTIL_ACK) or not isinstance(project, str):
        raise SwarmError("invalid_request")
    return {
        "broadcast_id": _name(draft.get("broadcast_id") or f"bc-{uuid.uuid4().hex[:16]}", empty=False),
        "channel": _name(draft.get("channel", "")),
        "target_role": _name(draft.get("target_role", "")),
        "brain_id": draft.get("brain_id"),
        "project_id": project,
        "severity": severity,
        "policy": policy,
        "message": message.strip(),
        "ttl_seconds": ttl,
    }


class FleetBroadcasts:
    """``authorize`` validates a scoped launch grant on every call; ``operator`` returns the authenticated operator."""

    def __init__(
        self,
        store: RedisStore,
        slug: str,
        authorize: Callable[[str], Registration],
        operator: Callable[[str], str],
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.store, self.slug, self.authorize, self.operator = store, slug, authorize, operator
        self.redis = store.redis
        self.clock = clock or (lambda: lease.now_ms(store))

    def key(self, *parts: str) -> str:
        return self.store.key(self.slug, *parts)

    def _grant(self, token: str) -> Registration:
        grant = self.authorize(token) if token else None
        if not isinstance(grant, Registration) or grant.swarm_id != self.slug:
            raise SwarmError("forbidden_scope")
        return grant

    def _transact(self, keys: list[str], decide: Callable) -> object:
        from redis.exceptions import WatchError

        for _ in range(WRITE_ATTEMPTS):
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(*keys)
                    writes, result = decide(pipe)
                    if writes:
                        pipe.multi()
                        writes(pipe)
                        pipe.execute()
                    return result
                except WatchError:
                    continue
        raise SwarmError("dependency_unavailable")

    def publish(self, token: str, draft: object, operation_id: str = "") -> Broadcast:
        """An agent publishes only into its grant's brain and one of its projects, never at operator-only severity."""
        fields = _draft(draft)
        grant = self._grant(token)
        fields["brain_id"] = fields["brain_id"] or grant.brain_id
        if fields["brain_id"] != grant.brain_id or fields["project_id"] not in grant.project_ids:
            raise SwarmError("forbidden_scope")
        if fields["severity"] in OPERATOR_ONLY:
            raise SwarmError("forbidden_scope")
        return self._commit(fields, f"{grant.seat_id}/{grant.execution_id}", operation_id)

    def publish_operator(self, token: str, draft: object, operation_id: str = "") -> Broadcast:
        fields = _draft(draft)
        name = self.operator(token) if token else None
        if not isinstance(name, str) or not name:
            raise SwarmError("unauthenticated")
        fields["brain_id"] = _name(fields["brain_id"], empty=False)
        return self._commit(fields, f"operator:{name}", operation_id)

    def _commit(self, fields: dict, author: str, operation_id: str) -> Broadcast:
        if operation_id:
            _name(operation_id, empty=False)
        canonical, operations, frozen = (
            self.key("broadcasts"),
            self.key("broadcast-operations"),
            self.key("broadcasts-frozen"),
        )
        now = self.clock()
        ttl = fields.pop("ttl_seconds")
        operation = f"{owner_of(author)}:{operation_id}"

        def decide(pipe):
            if pipe.exists(frozen):
                raise SwarmError("distribution_disabled")
            done = pipe.hget(operations, operation) if operation_id else None
            if done:
                replayed = decode(done)
                if replayed.content() != _content(fields) or replayed.expires_ms - replayed.published_ms != ttl * 1000:
                    raise SwarmError("invalid_request")
                return None, replayed
            raw = pipe.hget(canonical, fields["broadcast_id"])
            stored = decode(raw) if raw else None
            if stored and (owner_of(stored.author) != owner_of(author) or stored.brain_id != fields["brain_id"]):
                raise SwarmError("forbidden_scope")
            built = Broadcast(
                revision=stored.revision + 1 if stored else 1,
                fleet=self.slug,
                author=author,
                published_ms=now,
                expires_ms=now + ttl * 1000,
                **fields,
            )
            unchanged = stored and stored.expires_ms - stored.published_ms == ttl * 1000
            if unchanged and stored.content() == built.content() and now < stored.expires_ms:
                return None, stored

            def writes(pipe):
                pipe.hset(canonical, built.broadcast_id, encode(built))
                pipe.rpush(self.key("broadcast-history"), encode(built))
                if operation_id:
                    pipe.hset(operations, operation, encode(built))

            return writes, built

        return self._transact([canonical, operations, frozen], decide)

    def _matches(self, broadcast: Broadcast, grant: Registration, channels: list[str], now: int) -> bool:
        return (
            broadcast.fleet == self.slug
            and now < broadcast.expires_ms
            and broadcast.brain_id == grant.brain_id
            and (not broadcast.project_id or broadcast.project_id in grant.project_ids)
            and (not broadcast.target_role or broadcast.target_role == role_of(grant.seat_id))
            and (not broadcast.channel or broadcast.channel in channels or "*" in channels)
        )

    def claim(self, token: str, channels: list[str], claim_id: str = "") -> list[Delivery]:
        """Matching unexpired revisions this seat has not acknowledged; a ``once`` revision is handed out a single
        time per seat. Retrying with the same claim id returns the first answer without a second effect."""
        grant = self._grant(token)
        if claim_id:
            _name(claim_id, empty=False)
        seat, now = grant.seat_id, self.clock()
        records, claims = self.key("broadcast-deliveries", seat), self.key("broadcast-claims")

        def decide(pipe):
            last = json.loads(pipe.hget(claims, seat) or "{}")
            if claim_id and last.get("claim_id") == claim_id and last.get("generation") == grant.generation:
                return None, [_delivery(found) for found in last["deliveries"]]
            held = {name: json.loads(raw) for name, raw in pipe.hgetall(records).items()}
            if any(record["generation"] > grant.generation for record in held.values()):
                raise SwarmError("stale_generation")
            current = [decode(raw) for raw in pipe.hvals(self.key("broadcasts"))]
            found, changed = [], {}
            for broadcast in sorted(current, key=lambda item: (item.published_ms, item.broadcast_id)):
                if not self._matches(broadcast, grant, channels, now):
                    continue
                record = held.get(broadcast.broadcast_id)
                same = record is not None and record["revision"] == broadcast.revision
                if same and (record["acked"] or broadcast.policy == ONCE):
                    continue
                first = record["delivered_ms"] if same else now
                found.append(Delivery(broadcast, first, (first - broadcast.published_ms) / 1000))
                if not same or record["generation"] != grant.generation:
                    changed[broadcast.broadcast_id] = {
                        "revision": broadcast.revision,
                        "delivered_ms": first,
                        "acked": False,
                        "execution_id": grant.execution_id,
                        "generation": grant.generation,
                    }
            answer = {"claim_id": claim_id, "generation": grant.generation, "deliveries": [asdict(d) for d in found]}

            def writes(pipe):
                for name, record in changed.items():
                    pipe.hset(records, name, json.dumps(record))
                if claim_id:
                    pipe.hset(claims, seat, json.dumps(answer))

            return (writes if changed or claim_id else None), found

        return self._transact([records, claims, self.key("broadcasts")], decide)

    def acknowledge(self, token: str, broadcast_id: str, revision: int) -> bool:
        """False for a duplicate acknowledgement, which leaves the delivery record unchanged and is only counted."""
        grant = self._grant(token)
        records = self.key("broadcast-deliveries", grant.seat_id)

        def decide(pipe):
            raw = pipe.hget(records, broadcast_id)
            record = json.loads(raw) if raw else None
            if record is None or record["revision"] < revision:
                raise SwarmError("not_delivered")
            if record["revision"] > revision:
                raise SwarmError("stale_revision")
            if record["generation"] > grant.generation:
                raise SwarmError("stale_generation")
            if record["acked"]:
                return (lambda pipe: pipe.hincrby(self.key("broadcast-duplicate-acks"), grant.seat_id)), False
            acked = {**record, "acked": True, "execution_id": grant.execution_id, "generation": grant.generation}
            return (lambda pipe: pipe.hset(records, broadcast_id, json.dumps(acked))), True

        return self._transact([records], decide)

    def delivery(self, seat: str, broadcast_id: str) -> dict | None:
        raw = self.redis.hget(self.key("broadcast-deliveries", seat), broadcast_id)
        return json.loads(raw) if raw else None

    def broadcast_delivery_lag_seconds(self, seat: str, broadcast_id: str) -> float | None:
        record = self.delivery(seat, broadcast_id)
        stored = self.current(broadcast_id)
        if record is None or stored is None or stored.revision != record["revision"]:
            return None
        return (record["delivered_ms"] - stored.published_ms) / 1000

    def duplicate_acknowledgements(self, seat: str) -> int:
        return int(self.redis.hget(self.key("broadcast-duplicate-acks"), seat) or 0)

    def current(self, broadcast_id: str) -> Broadcast | None:
        raw = self.redis.hget(self.key("broadcasts"), broadcast_id)
        return decode(raw) if raw else None

    def history(self) -> list[Broadcast]:
        return [decode(raw) for raw in self.redis.lrange(self.key("broadcast-history"), 0, -1)]

    def freeze(self) -> None:
        self.redis.set(self.key("broadcasts-frozen"), "1")

    def thaw(self) -> None:
        self.redis.delete(self.key("broadcasts-frozen"))

    def frozen(self) -> bool:
        return bool(self.redis.exists(self.key("broadcasts-frozen")))


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def local_entry(delivery: Delivery, session_id: str) -> dict:
    """The local broadcast file shape of a revision claimed for one session; the hook delivers it to that session only."""
    broadcast = delivery.broadcast
    entry = {
        "id": f"{broadcast.fleet}:{broadcast.broadcast_id}:{broadcast.revision}:{session_id}",
        "message": broadcast.message,
        "severity": broadcast.severity,
        "persistent": broadcast.policy == UNTIL_ACK,
        "source": "fleet",
        "created_at": _iso(broadcast.published_ms),
        "ttl_seconds": (broadcast.expires_ms - broadcast.published_ms) // 1000,
        "expires_at": _iso(broadcast.expires_ms),
        "delivered_to": [],
        "fleet": {
            "swarm": broadcast.fleet,
            "broadcast_id": broadcast.broadcast_id,
            "revision": broadcast.revision,
            "session": session_id,
            "brain_id": broadcast.brain_id,
            "project_id": broadcast.project_id,
            "target_role": broadcast.target_role,
        },
    }
    if broadcast.channel:
        entry["channel"] = broadcast.channel
    return entry


def sync_local(
    fleet: FleetBroadcasts,
    token: str,
    session_id: str,
    channels: list[str],
    environ: Mapping[str, str],
    claim_id: str = "",
) -> int:
    """Claim this seat's fleet broadcasts into the local broadcast file; nothing while the fleet path is off."""
    if environ.get(FLAG) != "1":
        return 0
    from hooks.context.broadcast import cache_fleet_broadcasts

    found = fleet.claim(token, channels, claim_id)
    return cache_fleet_broadcasts([local_entry(delivery, session_id) for delivery in found])


def acknowledge_local(fleet: FleetBroadcasts, token: str, entry: dict) -> bool:
    """Carry a local acknowledgement of a cached fleet entry back to the fleet."""
    tag = entry.get("fleet")
    if not tag or tag["swarm"] != fleet.slug:
        raise SwarmError("forbidden_scope")
    return fleet.acknowledge(token, tag["broadcast_id"], tag["revision"])
