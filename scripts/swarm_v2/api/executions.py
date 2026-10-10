import json
import re
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from uuid import uuid4

from redis import Redis
from redis.client import Pipeline
from redis.exceptions import RedisError, WatchError

from scripts.swarm import lease
from scripts.swarm.store import RedisStore, SwarmError
from scripts.swarm_v2 import contracts
from scripts.swarm_v2.auth_context import IDENTIFIER, GrantRefused, LaunchAuthority, Registration
from scripts.swarm_v2.authority import RenewalFence, TaskAuthority, TaskClaim
from scripts.swarm_v2.runtime.operations import SPAWN, OperationJournal, digest

DEFAULT_LEASE_MS = 60_000
HEARTBEAT_LOCK_MS = 5_000
BEARER = "Bearer "
RELEASE = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end return 0"
WRITE_ATTEMPTS = 5
REGISTER = "/v2/executions/register"
HEARTBEAT = re.compile(r"/v2/executions/([^/]+)/heartbeat")
STATUS = {
    "invalid_request": 400,
    "unauthenticated": 401,
    "forbidden_scope": 403,
    "stale_generation": 409,
    "revision_conflict": 409,
    "dependency_unavailable": 503,
}
TASK_REFUSALS = {
    "stale_generation": ("stale_generation", "the task generation is no longer current"),
    "claim_held": ("stale_generation", "another execution holds this task"),
    "worker credential has expired": ("unauthenticated", "the worker credential has expired"),
    "out_of_order": ("revision_conflict", "a newer heartbeat already renewed this lease"),
}


def heartbeat_rejections(store: RedisStore, slug: str) -> dict[str, int]:
    rows = store.redis.hgetall(store.key(slug, "heartbeat-rejections"))
    return {error_class: int(count) for error_class, count in rows.items()}


def heartbeat_rejections_total(store: RedisStore, slug: str) -> int:
    return sum(heartbeat_rejections(store, slug).values())


class ExecutionsAPI:
    """Worker lifecycle endpoints; `tasks` must authorize tokens with `LaunchAuthority.bound`."""

    def __init__(self, grants: LaunchAuthority, tasks: TaskAuthority, lease_ms: int = DEFAULT_LEASE_MS) -> None:
        if type(lease_ms) is not int or lease_ms <= 0:
            raise ValueError("the server lease duration must be a positive number of milliseconds")
        self.grants, self.tasks, self.lease_ms = grants, tasks, lease_ms
        self.store, self.slug = tasks.store, tasks.slug
        self.contracts = contracts.load()

    def route(self, method: str, path: str, authorization: str, body: object) -> tuple[int, dict]:
        if not authorization.startswith(BEARER):
            return 401, GrantRefused("unauthenticated", "a bearer credential is required").detail()
        token = authorization.removeprefix(BEARER)
        subject = HEARTBEAT.fullmatch(path)
        try:
            if (method, path) == ("POST", REGISTER):
                return 200, self.register(token, body)
            if method == "PUT" and subject:
                return 200, self.heartbeat(subject[1], token, body)
        except GrantRefused as error:
            return STATUS[error.error_class], error.detail()
        except RedisError:
            return 503, GrantRefused("dependency_unavailable", "the execution store is unavailable").detail()
        return 404, GrantRefused("invalid_request", "no such execution endpoint").detail()

    def register(self, token: str, body: object) -> dict:
        registration = self.grants.register(self.slug, token, body)
        claim = _task(lambda: self.tasks.admit(token, self.lease_ms))
        record = self._record(self.store.redis, registration.execution_id)
        return {
            **self._ack(claim, record["archive_watermark"] if record else 0),
            "grant_id": registration.grant_id,
            "registered_at": registration.registered_at,
        }

    def heartbeat(self, execution_id: str, token: str, body: object) -> dict:
        try:
            return self._heartbeat(execution_id, token, body)
        except GrantRefused as error:
            error.operation_id = _operation_id(body.get("operation_id") if isinstance(body, Mapping) else None)
            self.store.redis.hincrby(self.store.key(self.slug, "heartbeat-rejections"), error.error_class)
            raise

    def _heartbeat(self, execution_id: str, token: str, body: object) -> dict:
        registration = self.grants.bound(self.slug, token)
        if execution_id != registration.execution_id:
            raise GrantRefused("forbidden_scope", "heartbeat names another execution")
        refusal = contracts.check(self.contracts, "heartbeat", body)
        if refusal:
            raise GrantRefused("invalid_request", refusal["detail"])
        self._bind(registration, body["authority"])
        fingerprint = digest(body)
        with self._serialized(execution_id):
            replay = _order(self._record(self.store.redis, execution_id), body["renewal_sequence"], fingerprint)
            if replay:
                return replay
            marker = self.store.key(self.slug, "heartbeat-sequence", execution_id)
            fence = RenewalFence(marker, body["renewal_sequence"], fingerprint)
            claim = _task(lambda: self.tasks.renew(token, body["authority"]["task_generation"], self.lease_ms, fence))
            return self._commit(claim, body, fingerprint)

    @contextmanager
    def _serialized(self, execution_id: str) -> Iterator[None]:
        key = self.store.key(self.slug, "heartbeat-lock", execution_id)
        holder = uuid4().hex
        if not self.store.redis.set(key, holder, nx=True, px=HEARTBEAT_LOCK_MS):
            raise GrantRefused("dependency_unavailable", "another heartbeat for this execution is in flight")
        try:
            yield
        finally:
            with suppress(RedisError):
                self.store.redis.eval(RELEASE, 1, key, holder)

    def _bind(self, registration: Registration, authority: dict) -> None:
        agent = self.store.execution(self.slug, registration.execution_id)
        claimed = (authority["execution_id"], authority["task_id"], authority["owner_identity"])
        if claimed != (registration.execution_id, registration.task_id, agent.name):
            raise GrantRefused("forbidden_scope", "heartbeat authority is outside its registered execution")
        held = self.tasks.controller.held
        if held is None:
            raise GrantRefused("dependency_unavailable", "the controller lease is absent")
        if authority["controller_epoch"] != held.epoch:
            raise GrantRefused("stale_generation", "heartbeat names another controller epoch")

    def _commit(self, claim: TaskClaim, body: dict, fingerprint: str) -> dict:
        key = self.store.key(self.slug, "heartbeats")
        for _ in range(WRITE_ATTEMPTS):
            with self.store.redis.pipeline() as pipe:
                try:
                    pipe.watch(key)
                    record = self._record(pipe, claim.execution_id)
                    replay = _order(record, body["renewal_sequence"], fingerprint)
                    if replay:
                        return replay
                    archive = max(body.get("archive_watermark", 0), record["archive_watermark"] if record else 0)
                    ack = {**self._ack(claim, archive), "renewal_sequence": body["renewal_sequence"]}
                    accepted = {
                        "renewal_sequence": body["renewal_sequence"],
                        "digest": fingerprint,
                        "state": body["state"],
                        "observed_at": body["observed_at"],
                        "resources": body.get("resources", {}),
                        "archive_watermark": archive,
                        "accepted_at_ms": lease.now_ms(self.store),
                        "ack": ack,
                    }
                    pipe.multi()
                    pipe.hset(key, claim.execution_id, json.dumps(accepted))
                    pipe.execute()
                    return ack
                except WatchError:
                    continue
        raise GrantRefused("dependency_unavailable", "heartbeats kept changing; the heartbeat was not recorded")

    def _record(self, reader: Redis | Pipeline, execution_id: str) -> dict | None:
        raw = reader.hget(self.store.key(self.slug, "heartbeats"), execution_id)
        return json.loads(raw) if raw else None

    def _ack(self, claim: TaskClaim, archive: int) -> dict:
        return {
            "schema_version": contracts.write_version(self.contracts, "heartbeat"),
            "execution_id": claim.execution_id,
            "task_id": claim.task_id,
            "task_generation": claim.generation,
            "controller_epoch": self.tasks.controller.held.epoch,
            "owner_identity": claim.holder,
            "lease_deadline_ms": claim.lease_deadline_ms,
            "lease_deadline": _timestamp(claim.lease_deadline_ms),
            "command_watermark": self.command_watermark(claim.execution_id),
            "archive_watermark": archive,
        }

    def command_watermark(self, execution_id: str) -> int:
        operations = OperationJournal(self.store).records(self.slug)
        return sum(
            1 for operation in operations if operation.execution_id == execution_id and operation.action != SPAWN
        )


def _task(action: Callable[[], TaskClaim]) -> TaskClaim:
    try:
        return action()
    except GrantRefused:
        raise
    except SwarmError as error:
        error_class, message = TASK_REFUSALS.get(str(error), ("dependency_unavailable", str(error)))
        raise GrantRefused(error_class, message) from error


def _order(record: dict | None, sequence: int, fingerprint: str) -> dict | None:
    if record is None or sequence > record["renewal_sequence"]:
        return None
    if sequence == record["renewal_sequence"] and fingerprint == record["digest"]:
        return record["ack"]
    raise GrantRefused("revision_conflict", "heartbeat renewal sequence is not newer than the accepted one")


def _operation_id(value: object) -> str:
    return value if isinstance(value, str) and IDENTIFIER.fullmatch(value) else "unknown"


def _timestamp(ms: int) -> str:
    seconds, millis = divmod(ms, 1000)
    return f"{time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(seconds))}.{millis:03d}Z"
