from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NoReturn
from uuid import uuid4

from hooks.context.project_sessions import SessionGrant
from scripts.swarm.store import SwarmError

if TYPE_CHECKING:
    from redis import Redis
    from redis.client import Pipeline

    from scripts.swarm.store import AgentRecord, RedisStore

SCHEMA_VERSION = "2.0"
TOKEN_PREFIX = "v2"
MIN_KEY_BYTES = 32
DEFAULT_TTL_SECONDS = 300
MAX_TTL_SECONDS = 900
WRITE_ATTEMPTS = 5
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
BOUND = ("swarm_id", "execution_id", "generation", "seat_id", "task_id", "account", "brain_id", "project_ids")
AUDIT = ("grant_id", "issuer", "audience", "key_id")
CLAIMS = {*BOUND, *AUDIT, "schema_version", "issued_at", "expires_at"}
NAMED = ("issuer", "audience", "key_id", "swarm_id", "execution_id", "seat_id", "task_id", "account", "brain_id")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,127}")
GRANT_ID = re.compile(r"lgr-[0-9a-f]{32}")
PROJECT_ID = re.compile(r"github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+|local:[A-Za-z0-9._-]+")


class GrantRefused(SwarmError):
    def __init__(self, error_class: str, message: str) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.operation_id = "unknown"
        self.retry = "same_request" if error_class == "dependency_unavailable" else "new_request"

    def detail(self) -> dict:
        return {
            "error_class": self.error_class,
            "operation_id": self.operation_id,
            "retry": self.retry,
            "message": str(self),
        }


@dataclass(frozen=True)
class LaunchKey:
    key_id: str
    secret: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not IDENTIFIER.fullmatch(self.key_id):
            raise ValueError("signing key ID must be an identifier")
        if len(self.secret) < MIN_KEY_BYTES:
            raise ValueError(f"signing key must be at least {MIN_KEY_BYTES} bytes")


@dataclass(frozen=True)
class Registration:
    grant_id: str
    swarm_id: str
    execution_id: str
    generation: int
    seat_id: str
    task_id: str
    account: str
    brain_id: str
    project_ids: list[str]
    issuer: str
    audience: str
    key_id: str
    registered_at: str

    def session_grant(self) -> SessionGrant:
        return SessionGrant(frozenset(self.project_ids))


def launch_grant_rejections(store: RedisStore, slug: str) -> dict[str, int]:
    rows = store.redis.hgetall(store.key(slug, "launch-grant-rejections"))
    return {error_class: int(count) for error_class, count in rows.items()}


def launch_grant_rejections_total(store: RedisStore, slug: str) -> int:
    return sum(launch_grant_rejections(store, slug).values())


class LaunchAuthority:
    def __init__(
        self,
        store: RedisStore,
        key: LaunchKey,
        issuer: str,
        audience: str,
        clock: Callable[[], float] = time.time,
        ttl: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        if not 0 < ttl <= MAX_TTL_SECONDS:
            raise ValueError(f"launch grant lifetime must be between 1 and {MAX_TTL_SECONDS} seconds")
        self.store = store
        self.redis = store.redis
        self.key = key
        self.issuer = issuer
        self.audience = audience
        self.clock = clock
        self.ttl = ttl

    def refuse(self, slug: str, error_class: str, message: str) -> NoReturn:
        self.redis.hincrby(self.store.key(slug, "launch-grant-rejections"), error_class)
        raise GrantRefused(error_class, message)

    def issue(self, slug: str, execution_id: str, *, project_ids: list[str], brain_id: str, account: str) -> str:
        execution = self.store.execution(slug, execution_id)
        if not _identifier(execution.task) or not _identifier(execution.seat):
            self.refuse(slug, "invalid_request", "execution task or seat is not an identifier")
        if not isinstance(project_ids, list | tuple) or not project_ids:
            self.refuse(slug, "invalid_request", "a launch grant needs at least one project")
        if not all(_project(project) for project in project_ids):
            self.refuse(slug, "invalid_request", "launch grant project is not a canonical project ID")
        if not _identifier(brain_id):
            self.refuse(slug, "invalid_request", "launch grant brain is not an identifier")
        if not _identifier(account):
            self.refuse(slug, "invalid_request", "launch grant account is not an identifier")
        now = int(self.clock())
        claims = {
            "schema_version": SCHEMA_VERSION,
            "grant_id": f"lgr-{uuid4().hex}",
            "issuer": self.issuer,
            "audience": self.audience,
            "key_id": self.key.key_id,
            "swarm_id": slug,
            "execution_id": execution.execution_id,
            "generation": execution.generation,
            "seat_id": execution.seat,
            "task_id": execution.task,
            "account": account,
            "brain_id": brain_id,
            "project_ids": sorted(set(project_ids)),
            "issued_at": _timestamp(now),
            "expires_at": _timestamp(now + self.ttl),
        }
        audit = {name: claims[name] for name in (*AUDIT, "execution_id", "generation", "issued_at", "expires_at")}
        self.record(slug, execution, {**audit, "state": "issued"})
        return self.sign(claims)

    def record(self, slug: str, execution: AgentRecord, audit: dict) -> None:
        from redis.exceptions import WatchError

        disabled = self.store.key(slug, "launch-grants-disabled")
        for _ in range(WRITE_ATTEMPTS):
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(disabled, self.store.key(slug, "executions"))
                    if pipe.exists(disabled):
                        self.refuse(slug, "forbidden_scope", "launch grants are disabled for this swarm")
                    occupants = self.store.execution_occupants(slug).values()
                    if execution.execution_id not in {occupant.execution_id for occupant in occupants}:
                        self.refuse(slug, "stale_generation", "execution is not the current attempt of its seat")
                    pipe.multi()
                    pipe.hset(self.store.key(slug, "launch-grants"), audit["grant_id"], json.dumps(audit))
                    pipe.execute()
                    return
                except WatchError:
                    continue
        self.refuse(slug, "dependency_unavailable", "launch grants kept changing; the grant was not issued")

    def sign(self, claims: Mapping) -> str:
        payload = _encode(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
        signed = f"{TOKEN_PREFIX}.{payload}"
        return f"{signed}.{_encode(hmac.new(self.key.secret, signed.encode(), hashlib.sha256).digest())}"

    def _verify(self, slug: str, token: str) -> dict:
        parts = token.split(".") if isinstance(token, str) else []
        if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
            self.refuse(slug, "unauthenticated", "launch grant is malformed")
        expected = hmac.new(self.key.secret, f"{parts[0]}.{parts[1]}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_encode(expected).encode(), parts[2].encode()):
            self.refuse(slug, "unauthenticated", "launch grant signature is invalid")
        claims = _decode(parts[1])
        if not isinstance(claims, dict):
            self.refuse(slug, "unauthenticated", "launch grant is malformed")
        if claims.get("schema_version") != SCHEMA_VERSION:
            self.refuse(slug, "invalid_request", "unsupported launch grant version")
        if set(claims) != CLAIMS or not _claims_valid(claims):
            self.refuse(slug, "unauthenticated", "launch grant is malformed")
        self.check_claims(slug, claims)
        return claims

    def check_claims(self, slug: str, claims: dict) -> None:
        if claims["key_id"] != self.key.key_id:
            self.refuse(slug, "unauthenticated", "launch grant names another signing key")
        if claims["issuer"] != self.issuer:
            self.refuse(slug, "unauthenticated", "launch grant is from another issuer")
        if claims["audience"] != self.audience:
            self.refuse(slug, "unauthenticated", "launch grant is for another audience")
        now = int(self.clock())
        if now < _seconds(claims["issued_at"]):
            self.refuse(slug, "unauthenticated", "launch grant is not yet valid")
        if now >= _seconds(claims["expires_at"]):
            self.refuse(slug, "unauthenticated", "launch grant has expired")
        if claims["swarm_id"] != slug:
            self.refuse(slug, "forbidden_scope", "launch grant belongs to another swarm")

    def check_body(self, slug: str, claims: dict, body: object) -> None:
        if not isinstance(body, Mapping):
            self.refuse(slug, "invalid_request", "registration body must be an object")
        if set(body) - set(BOUND):
            self.refuse(slug, "invalid_request", "registration names an unsupported identity field")
        if not {"execution_id", "generation"} <= set(body):
            self.refuse(slug, "invalid_request", "registration must name its execution and generation")
        for name in BOUND:
            if name in body and (type(body[name]) is not type(claims[name]) or body[name] != claims[name]):
                self.refuse(slug, "forbidden_scope", f"registration {name} is outside the launch grant")

    def register(self, slug: str, token: str, body: object, operation_id: str = "") -> Registration:
        try:
            return self.commit(slug, token, body)
        except GrantRefused as error:
            error.operation_id = operation_id if _identifier(operation_id) else "unknown"
            raise

    def commit(self, slug: str, token: str, body: object) -> Registration:
        from redis.exceptions import WatchError

        claims = self._verify(slug, token)
        self.check_body(slug, claims, body)
        parts = ("executions", "launch-registrations", "launch-grants", "launch-grants-disabled")
        keys = [self.store.key(slug, part) for part in parts]
        for _ in range(WRITE_ATTEMPTS):
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(*keys)
                    return self.admit(pipe, slug, claims)
                except WatchError:
                    continue
        self.refuse(
            slug, "dependency_unavailable", "launch registrations kept changing; registration was not committed"
        )

    def issued(self, reader: Redis | Pipeline, slug: str, claims: dict) -> dict:
        raw = reader.hget(self.store.key(slug, "launch-grants"), claims["grant_id"])
        if not raw:
            self.refuse(slug, "unauthenticated", "launch grant was not issued by this controller")
        audit = json.loads(raw)
        if audit["state"] == "revoked":
            self.refuse(slug, "unauthenticated", "launch grant was revoked")
        return audit

    def verify(self, slug: str, token: str) -> Registration:
        """Checks the grant without registering it and without judging currency; issue and register judge that."""
        claims = self._verify(slug, token)
        self.issued(self.redis, slug, claims)
        return _registration(claims, "")

    def admit(self, pipe: Pipeline, slug: str, claims: dict) -> Registration:
        grants = self.store.key(slug, "launch-grants")
        registrations = self.store.key(slug, "launch-registrations")
        audit = self.issued(pipe, slug, claims)
        occupants = self.store.execution_occupants(slug).values()
        if (claims["execution_id"], claims["generation"]) not in {(a.execution_id, a.generation) for a in occupants}:
            self.refuse(slug, "stale_generation", "launch grant is for a superseded execution")
        existing = pipe.hget(registrations, claims["execution_id"])
        if existing:
            registration = Registration(**json.loads(existing))
            if registration.grant_id != claims["grant_id"]:
                self.refuse(slug, "forbidden_scope", "execution is already registered under another launch grant")
            return registration
        if pipe.exists(self.store.key(slug, "launch-grants-disabled")):
            self.refuse(slug, "forbidden_scope", "launch grants are disabled for this swarm")
        registration = _registration(claims, _timestamp(int(self.clock())))
        pipe.multi()
        pipe.hset(registrations, claims["execution_id"], json.dumps(asdict(registration)))
        pipe.hset(grants, claims["grant_id"], json.dumps({**audit, "state": "registered"}))
        pipe.execute()
        return registration

    def registration(self, slug: str, execution_id: str) -> Registration | None:
        raw = self.redis.hget(self.store.key(slug, "launch-registrations"), execution_id)
        return Registration(**json.loads(raw)) if raw else None

    def disable(self, slug: str) -> list[str]:
        from redis.exceptions import WatchError

        key = self.store.key(slug, "launch-grants")
        for _ in range(WRITE_ATTEMPTS):
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(key)
                    rows = {grant_id: json.loads(raw) for grant_id, raw in pipe.hgetall(key).items()}
                    revoked = sorted(grant_id for grant_id, audit in rows.items() if audit["state"] == "issued")
                    pipe.multi()
                    pipe.set(self.store.key(slug, "launch-grants-disabled"), _timestamp(int(self.clock())))
                    for grant_id in revoked:
                        pipe.hset(key, grant_id, json.dumps({**rows[grant_id], "state": "revoked"}))
                    pipe.execute()
                    return revoked
                except WatchError:
                    continue
        self.refuse(slug, "dependency_unavailable", "launch grants kept changing; revocation was not committed")

    def enable(self, slug: str) -> None:
        self.redis.delete(self.store.key(slug, "launch-grants-disabled"))


def _registration(claims: dict, registered_at: str) -> Registration:
    return Registration(**{name: claims[name] for name in (*BOUND, *AUDIT)}, registered_at=registered_at)


def _identifier(value: object) -> bool:
    return isinstance(value, str) and bool(IDENTIFIER.fullmatch(value))


def _project(value: object) -> bool:
    return isinstance(value, str) and bool(PROJECT_ID.fullmatch(value))


def _claims_valid(claims: dict) -> bool:
    projects = claims["project_ids"]
    return (
        all(_identifier(claims[name]) for name in NAMED)
        and isinstance(claims["grant_id"], str)
        and bool(GRANT_ID.fullmatch(claims["grant_id"]))
        and type(claims["generation"]) is int
        and claims["generation"] > 0
        and isinstance(projects, list)
        and bool(projects)
        and all(_project(project) for project in projects)
        and projects == sorted(set(projects))
        and _seconds(claims["issued_at"]) is not None
        and _seconds(claims["expires_at"]) is not None
    )


def _timestamp(seconds: int) -> str:
    return datetime.fromtimestamp(seconds, UTC).strftime(TIME_FORMAT)


def _seconds(value: object) -> int | None:
    try:
        return int(datetime.strptime(value, TIME_FORMAT).replace(tzinfo=UTC).timestamp())
    except (TypeError, ValueError):
        return None


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _decode(payload: str) -> object:
    try:
        return json.loads(base64.urlsafe_b64decode(payload + "=="))
    except (binascii.Error, ValueError):
        return None
