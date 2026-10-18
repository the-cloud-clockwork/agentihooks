import json
import re
from collections.abc import Callable
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from redis.exceptions import RedisError, WatchError

from scripts.swarm import lease
from scripts.swarm.store import RedisStore
from scripts.swarm_v2.api.executions import BEARER, STATUS
from scripts.swarm_v2.auth_context import GrantRefused, LaunchAuthority
from scripts.swarm_v2.runtime.operations import digest

KINDS = frozenset(("drain", "cancel", "answer", "stop"))
OUTCOMES = {"drain": frozenset(("checkpointed", "failed"))}
SETTLED = frozenset(("succeeded", "failed", "not_run"))
MAX_EXPIRY_MS = 900_000
DEFAULT_POLL_INTERVAL_MS = 1000
DEFAULT_POLL_LIMIT = 10
WRITE_ATTEMPTS = 5
ACK_LAG_SAMPLES = 1000
ISSUED, ACCEPTED, COMPLETED, EXPIRED = "issued", "accepted", "completed", "expired"
POLL = re.compile(r"/v2/executions/([^/]+)/commands")
ACTION = re.compile(r"/v2/executions/([^/]+)/commands/([^/]+)/(ack|complete)")
STATUSES = {**STATUS, "not_found": 404, "expired": 410, "rate_limited": 429}


def worker_command_ack_lag_seconds(store: RedisStore, slug: str) -> list[float]:
    return [int(ms) / 1000 for ms in store.redis.lrange(store.key(slug, "worker-command-ack-lag"), 0, -1)]


def _view(record: dict, now: int) -> dict:
    expired = record["state"] == ISSUED and now >= record["expires_at_ms"]
    return {**record, "state": EXPIRED if expired else record["state"]}


def _check(kind: object, payload: object, key: object, expires_in_ms: object) -> None:
    if kind not in KINDS:
        raise GrantRefused("invalid_request", "unknown command kind")
    if kind == "answer":
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str) or not text or set(payload) != {"text"}:
            raise GrantRefused("invalid_request", "an answer needs non-empty text and nothing else")
    elif payload != {}:
        raise GrantRefused("invalid_request", "this command kind takes no payload")
    if not isinstance(key, str) or not key:
        raise GrantRefused("invalid_request", "a command key is required")
    if type(expires_in_ms) is not int or not 0 < expires_in_ms <= MAX_EXPIRY_MS:
        raise GrantRefused("invalid_request", f"the expiry must be between 1 and {MAX_EXPIRY_MS} milliseconds")


def _fits(kind: str, outcome: object) -> bool:
    if not isinstance(outcome, dict) or outcome.get("status") not in OUTCOMES.get(kind, SETTLED):
        return False
    checkpoint = outcome.get("checkpoint")
    return outcome["status"] != "checkpointed" or (isinstance(checkpoint, str) and bool(checkpoint))


class CommandQueue:
    """Controller side of worker commands: issued, accepted and completed are explicit stored states."""

    def __init__(self, store: RedisStore, slug: str, kinds: frozenset[str] = KINDS) -> None:
        self.store, self.slug, self.kinds = store, slug, kinds

    def key(self, execution_id: str) -> str:
        return self.store.key(self.slug, "worker-commands", execution_id)

    def command_id(self, execution_id: str, key: str) -> str:
        return f"cmd-{uuid5(NAMESPACE_URL, json.dumps([self.slug, execution_id, key])).hex}"

    def issue(self, execution_id: str, generation: int, kind: str, payload: dict, key: str, expires_in_ms: int) -> dict:
        _check(kind, payload, key, expires_in_ms)
        occupants = self.store.execution_occupants(self.slug).values()
        if (execution_id, generation) not in {(agent.execution_id, agent.generation) for agent in occupants}:
            raise GrantRefused("stale_generation", "the execution is not the current occupant of its seat")
        command_id = self.command_id(execution_id, key)
        payload_digest = digest({"kind": kind, "payload": payload})

        def create(record: dict | None, now: int) -> tuple[dict, None]:
            if record is not None:
                if (record["kind"], record["payload_digest"], record["generation"]) != (
                    kind,
                    payload_digest,
                    generation,
                ):
                    raise GrantRefused("revision_conflict", "the command key was issued with another payload")
                return record, None
            if kind not in self.kinds:
                raise GrantRefused("forbidden_scope", "new commands of this kind are not issued")
            return {
                "command_id": command_id,
                "execution_id": execution_id,
                "generation": generation,
                "kind": kind,
                "payload": payload,
                "payload_digest": payload_digest,
                "issued_at_ms": now,
                "expires_at_ms": now + expires_in_ms,
                "state": ISSUED,
                "accepted_at_ms": None,
                "completed_at_ms": None,
                "outcome": None,
            }, None

        return self._update(execution_id, command_id, create)

    def outcome(self, execution_id: str, command_id: str) -> dict | None:
        raw = self.store.redis.hget(self.key(execution_id), command_id)
        return _view(json.loads(raw), lease.now_ms(self.store)) if raw else None

    def pending(self, execution_id: str, limit: int) -> list[dict]:
        now = lease.now_ms(self.store)
        records = [_view(json.loads(raw), now) for raw in self.store.redis.hvals(self.key(execution_id))]
        live = [record for record in records if record["state"] in (ISSUED, ACCEPTED)]
        return sorted(live, key=lambda record: record["issued_at_ms"])[:limit]

    def accept(self, execution_id: str, command_id: str, payload_digest: str) -> dict:
        def change(record: dict | None, now: int) -> tuple[dict, int | None]:
            record = _found(record)
            if record["payload_digest"] != payload_digest:
                raise GrantRefused("revision_conflict", "the acknowledgement names another payload")
            if record["state"] != ISSUED:
                return record, None
            if now >= record["expires_at_ms"]:
                raise GrantRefused("expired", "the command expired before it was acknowledged")
            return {**record, "state": ACCEPTED, "accepted_at_ms": now}, now - record["issued_at_ms"]

        return self._update(execution_id, command_id, change)

    def complete(self, execution_id: str, command_id: str, outcome: object) -> dict:
        def change(record: dict | None, now: int) -> tuple[dict, None]:
            record = _found(record)
            if not _fits(record["kind"], outcome):
                raise GrantRefused("invalid_request", "the outcome does not fit the command kind")
            if record["state"] == COMPLETED:
                if record["outcome"] == outcome:
                    return record, None
                raise GrantRefused("revision_conflict", "the command already completed with another outcome")
            if _view(record, now)["state"] == EXPIRED:
                raise GrantRefused("expired", "the command expired before it was acknowledged")
            if record["state"] != ACCEPTED:
                raise GrantRefused("revision_conflict", "the command was not acknowledged")
            return {**record, "state": COMPLETED, "completed_at_ms": now, "outcome": outcome}, None

        return self._update(execution_id, command_id, change)

    def _update(self, execution_id: str, command_id: str, change: Callable[[Any, int], tuple[dict, Any]]) -> dict:
        key = self.key(execution_id)
        for _ in range(WRITE_ATTEMPTS):
            with self.store.redis.pipeline() as pipe:
                try:
                    pipe.watch(key)
                    raw = pipe.hget(key, command_id)
                    now = lease.now_ms(self.store)
                    record, lag = change(json.loads(raw) if raw else None, now)
                    pipe.multi()
                    pipe.hset(key, command_id, json.dumps(record))
                    if lag is not None:
                        samples = self.store.key(self.slug, "worker-command-ack-lag")
                        pipe.rpush(samples, lag)
                        pipe.ltrim(samples, -ACK_LAG_SAMPLES, -1)
                    pipe.execute()
                    return _view(record, now)
                except WatchError:
                    continue
        raise GrantRefused("dependency_unavailable", "commands kept changing; nothing was recorded")


def _found(record: dict | None) -> dict:
    if record is None:
        raise GrantRefused("not_found", "no such command for this execution")
    return record


class CommandsAPI:
    """Worker command endpoints; a worker sees only its own execution's commands."""

    def __init__(
        self,
        grants: LaunchAuthority,
        queue: CommandQueue,
        poll_interval_ms: int = DEFAULT_POLL_INTERVAL_MS,
        poll_limit: int = DEFAULT_POLL_LIMIT,
    ) -> None:
        if any(type(value) is not int or value <= 0 for value in (poll_interval_ms, poll_limit)):
            raise ValueError("poll interval and limit must be positive integers")
        self.grants, self.queue = grants, queue
        self.poll_interval_ms, self.poll_limit = poll_interval_ms, poll_limit

    def route(self, method: str, path: str, authorization: str, body: object) -> tuple[int, dict]:
        if not authorization.startswith(BEARER):
            return 401, GrantRefused("unauthenticated", "a bearer credential is required").detail()
        token = authorization.removeprefix(BEARER)
        polled, acted = POLL.fullmatch(path), ACTION.fullmatch(path)
        try:
            if method == "GET" and polled:
                return 200, self.poll(polled[1], token)
            if method == "POST" and acted:
                return 200, self.act(acted[1], acted[2], acted[3], token, body)
        except GrantRefused as error:
            return STATUSES[error.error_class], error.detail()
        except RedisError:
            return 503, GrantRefused("dependency_unavailable", "the command store is unavailable").detail()
        return 404, GrantRefused("invalid_request", "no such command endpoint").detail()

    def _own(self, execution_id: str, token: str) -> None:
        if self.grants.bound(self.queue.slug, token).execution_id != execution_id:
            raise GrantRefused("forbidden_scope", "the path names another execution")

    def poll(self, execution_id: str, token: str) -> dict:
        self._own(execution_id, token)
        self._stamp(execution_id)
        return {
            "execution_id": execution_id,
            "commands": self.queue.pending(execution_id, self.poll_limit),
            "poll_interval_ms": self.poll_interval_ms,
        }

    def _stamp(self, execution_id: str) -> None:
        store = self.queue.store
        key = store.key(self.queue.slug, "worker-command-polls")
        with store.redis.pipeline() as pipe:
            try:
                pipe.watch(key)
                now = lease.now_ms(store)
                last = pipe.hget(key, execution_id)
                wait = 0 if last is None else int(last) + self.poll_interval_ms - now
                if wait <= 0:
                    pipe.multi()
                    pipe.hset(key, execution_id, now)
                    pipe.execute()
                    return
            except WatchError:
                wait = self.poll_interval_ms
        error = GrantRefused("rate_limited", f"poll again in {wait} ms")
        error.retry = "same_request"
        raise error

    def act(self, execution_id: str, command_id: str, action: str, token: str, body: object) -> dict:
        self._own(execution_id, token)
        fields = body if isinstance(body, dict) else {}
        if action == "complete":
            return self.queue.complete(execution_id, command_id, fields.get("outcome"))
        payload_digest = fields.get("payload_digest")
        if not isinstance(payload_digest, str):
            raise GrantRefused("invalid_request", "an acknowledgement needs the payload digest")
        return self.queue.accept(execution_id, command_id, payload_digest)
