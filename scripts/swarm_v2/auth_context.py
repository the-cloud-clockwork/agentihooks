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
from typing import TYPE_CHECKING
from uuid import uuid4

from hooks.context.project_sessions import SessionGrant
from scripts.swarm.store import SwarmError

if TYPE_CHECKING:
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
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,127}")
PROJECT_ID = re.compile(r"github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+|local:[A-Za-z0-9._-]+")


class GrantRefused(SwarmError):
    def __init__(self, error_class: str, message: str) -> None:
        super().__init__(message)
        self.error_class = error_class


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
    project_ids: list
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

    def refuse(self, slug: str, error_class: str, message: str) -> None:
        self.redis.hincrby(self.store.key(slug, "launch-grant-rejections"), error_class, 1)
        raise GrantRefused(error_class, message)

    def issue(self, slug: str, execution_id: str, *, project_ids: list, brain_id: str, account: str) -> str:
        execution = self.store.execution(slug, execution_id)
        projects = sorted(set(project_ids))
        if not projects:
            self.refuse(slug, "invalid_request", "a launch grant needs at least one project")
        if not all(isinstance(project, str) and PROJECT_ID.fullmatch(project) for project in projects):
            self.refuse(slug, "invalid_request", "launch grant project is not a canonical project ID")
        if not _identifier(brain_id):
            self.refuse(slug, "invalid_request", "launch grant brain is not an identifier")
        if not _identifier(account):
            self.refuse(slug, "invalid_request", "launch grant account is not an identifier")
        if not self.current(slug, execution):
            self.refuse(slug, "stale_generation", "execution is not the current attempt of its seat")
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
            "project_ids": projects,
            "issued_at": _timestamp(now),
            "expires_at": _timestamp(now + self.ttl),
        }
        audit = {name: claims[name] for name in (*AUDIT, "execution_id")}
        audit.update(generation=execution.generation, issued_at=claims["issued_at"], expires_at=claims["expires_at"])
        self.redis.hset(
            self.store.key(slug, "launch-grants"), claims["grant_id"], json.dumps({**audit, "state": "issued"})
        )
        return self.sign(claims)

    def sign(self, claims: Mapping) -> str:
        payload = _encode(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
        signed = f"{TOKEN_PREFIX}.{payload}"
        return f"{signed}.{_encode(hmac.new(self.key.secret, signed.encode(), hashlib.sha256).digest())}"

    def verify(self, slug: str, token: str) -> dict:
        parts = token.split(".") if isinstance(token, str) else []
        if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
            self.refuse(slug, "unauthenticated", "launch grant is malformed")
        expected = hmac.new(self.key.secret, f"{parts[0]}.{parts[1]}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_encode(expected), parts[2]):
            self.refuse(slug, "unauthenticated", "launch grant signature is invalid")
        claims = _decode(parts[1])
        if not isinstance(claims, dict):
            self.refuse(slug, "unauthenticated", "launch grant is malformed")
        if claims.get("schema_version") != SCHEMA_VERSION:
            self.refuse(slug, "invalid_request", "unsupported launch grant version")
        if set(claims) != CLAIMS:
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
        if not self.redis.hexists(self.store.key(slug, "launch-grants"), claims["grant_id"]):
            self.refuse(slug, "unauthenticated", "launch grant was not issued by this controller")

    def check_body(self, slug: str, claims: dict, body: object) -> None:
        if not isinstance(body, Mapping):
            self.refuse(slug, "invalid_request", "registration body must be an object")
        if set(body) - set(BOUND):
            self.refuse(slug, "invalid_request", "registration names an unsupported identity field")
        if not {"execution_id", "generation"} <= set(body):
            self.refuse(slug, "invalid_request", "registration must name its execution and generation")
        for name in BOUND:
            if name in body and body[name] != claims[name]:
                self.refuse(slug, "forbidden_scope", f"registration {name} is outside the launch grant")

    def register(self, slug: str, token: str, body: object) -> Registration:
        from redis.exceptions import WatchError

        claims = self.verify(slug, token)
        self.check_body(slug, claims, body)
        executions = self.store.key(slug, "executions")
        registrations = self.store.key(slug, "launch-registrations")
        grants = self.store.key(slug, "launch-grants")
        for _ in range(WRITE_ATTEMPTS):
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(executions, registrations, grants)
                    audit = json.loads(pipe.hget(grants, claims["grant_id"]))
                    if audit["state"] == "revoked":
                        self.refuse(slug, "unauthenticated", "launch grant was revoked")
                    current = self.store.execution_registry.occupants(slug, pipe).get(claims["seat_id"])
                    if not current or (current.execution_id, current.generation) != (
                        claims["execution_id"],
                        claims["generation"],
                    ):
                        self.refuse(slug, "stale_generation", "launch grant is for a superseded execution")
                    existing = pipe.hget(registrations, claims["execution_id"])
                    if existing:
                        registration = Registration(**json.loads(existing))
                        if registration.grant_id != claims["grant_id"]:
                            self.refuse(
                                slug, "forbidden_scope", "execution is already registered under another launch grant"
                            )
                        return registration
                    registration = Registration(
                        **{name: claims[name] for name in (*BOUND, *AUDIT)},
                        registered_at=_timestamp(int(self.clock())),
                    )
                    pipe.multi()
                    pipe.hset(registrations, claims["execution_id"], json.dumps(asdict(registration)))
                    pipe.hset(grants, claims["grant_id"], json.dumps({**audit, "state": "registered"}))
                    pipe.execute()
                    return registration
                except WatchError:
                    continue
        raise SwarmError("launch registrations kept changing; registration was not committed")

    def registration(self, slug: str, execution_id: str) -> Registration | None:
        raw = self.redis.hget(self.store.key(slug, "launch-registrations"), execution_id)
        return Registration(**json.loads(raw)) if raw else None

    def revoke_outstanding(self, slug: str) -> list[str]:
        from redis.exceptions import WatchError

        key = self.store.key(slug, "launch-grants")
        for _ in range(WRITE_ATTEMPTS):
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(key)
                    rows = {grant_id: json.loads(raw) for grant_id, raw in pipe.hgetall(key).items()}
                    revoked = sorted(grant_id for grant_id, audit in rows.items() if audit["state"] == "issued")
                    pipe.multi()
                    for grant_id in revoked:
                        pipe.hset(key, grant_id, json.dumps({**rows[grant_id], "state": "revoked"}))
                    pipe.execute()
                    return revoked
                except WatchError:
                    continue
        raise SwarmError("launch grants kept changing; revocation was not committed")

    def current(self, slug: str, execution: AgentRecord) -> bool:
        occupant = self.store.execution_occupants(slug).get(execution.seat)
        return bool(occupant) and occupant.execution_id == execution.execution_id


def _identifier(value: object) -> bool:
    return isinstance(value, str) and bool(IDENTIFIER.fullmatch(value))


def _timestamp(seconds: int) -> str:
    return datetime.fromtimestamp(seconds, UTC).strftime(TIME_FORMAT)


def _seconds(value: str) -> int:
    return int(datetime.strptime(value, TIME_FORMAT).replace(tzinfo=UTC).timestamp())


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _decode(payload: str) -> object:
    try:
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (binascii.Error, ValueError):
        return None
