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


def _revoked(audit: dict) -> dict | None:
    return None if audit["state"] == "revoked" else {**audit, "state": "revoked"}


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
        def change(doc):
            if doc["grants_disabled"]:
                self.refuse(slug, "forbidden_scope", "launch grants are disabled for this swarm")
            occupants = self.store.execution_registry.occupants(slug, doc).values()
            if execution.execution_id not in {occupant.execution_id for occupant in occupants}:
                self.refuse(slug, "stale_generation", "execution is not the current attempt of its seat")
            doc["grants"][audit["grant_id"]] = audit

        self._write(slug, change, "launch grants kept changing; the grant was not issued")

    def _write(self, slug: str, change: Callable[[dict], object], contention: str):
        from scripts.swarm import fencing

        try:
            return fencing.write(self.store, slug, change)
        except SwarmError as error:
            if str(error) != fencing.CONTENTION:
                raise
        self.refuse(slug, "dependency_unavailable", contention)

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
        claims = self._verify(slug, token)
        self.check_body(slug, claims, body)
        return self._write(
            slug,
            lambda doc: self.admit(doc, slug, claims),
            "launch registrations kept changing; registration was not committed",
        )

    def issued(self, doc: dict, slug: str, claims: dict) -> dict:
        audit = doc["grants"].get(claims["grant_id"])
        if not audit:
            self.refuse(slug, "unauthenticated", "launch grant was not issued by this controller")
        if audit["state"] == "revoked":
            self.refuse(slug, "unauthenticated", "launch grant was revoked")
        return audit

    def verify(self, slug: str, token: str) -> Registration:
        """Checks the grant without registering it and without judging currency; issue and register judge that."""
        from scripts.swarm import fencing

        claims = self._verify(slug, token)
        self.issued(fencing.read(self.store, slug), slug, claims)
        return _registration(claims, "")

    def admit(self, doc: dict, slug: str, claims: dict) -> Registration:
        audit = self.issued(doc, slug, claims)
        occupants = self.store.execution_registry.occupants(slug, doc).values()
        if (claims["execution_id"], claims["generation"]) not in {(a.execution_id, a.generation) for a in occupants}:
            self.refuse(slug, "stale_generation", "launch grant is for a superseded execution")
        row = doc["executions"].get(claims["execution_id"]) or doc["archive"].get(claims["execution_id"])
        if row["state"] == "ended":
            self.refuse(slug, "stale_generation", "launch grant is for an ended execution")
        existing = doc["registrations"].get(claims["execution_id"])
        if existing:
            registration = Registration(**existing)
            if registration.grant_id != claims["grant_id"]:
                self.refuse(slug, "forbidden_scope", "execution is already registered under another launch grant")
            return registration
        if doc["grants_disabled"]:
            self.refuse(slug, "forbidden_scope", "launch grants are disabled for this swarm")
        registration = _registration(claims, _timestamp(int(self.clock())))
        doc["registrations"][claims["execution_id"]] = asdict(registration)
        doc["grants"][claims["grant_id"]] = {**audit, "state": "registered"}
        return registration

    def registration(self, slug: str, execution_id: str, doc: dict | None = None) -> Registration | None:
        from scripts.swarm import fencing

        row = (doc if doc is not None else fencing.read(self.store, slug))["registrations"].get(execution_id)
        return Registration(**row) if row else None

    def bound(self, slug: str, token: str) -> Registration:
        from scripts.swarm import fencing

        claims = self._verify(slug, token)
        doc = fencing.read(self.store, slug)
        registration = self.registration(slug, claims["execution_id"], doc)
        if registration is None or registration.grant_id != claims["grant_id"]:
            self.refuse(slug, "unauthenticated", "launch grant is not registered")
        if self._audit(doc, slug, registration.grant_id)["state"] == "revoked":
            self.refuse(slug, "unauthenticated", "launch grant was revoked")
        occupants = self.store.execution_registry.occupants(slug, doc).values()
        if (claims["execution_id"], claims["generation"]) not in {(a.execution_id, a.generation) for a in occupants}:
            self.refuse(slug, "stale_generation", "launch grant is for a superseded execution")
        return registration

    def renew(self, slug: str, token: str) -> tuple[str, str]:
        registration = self.bound(slug, token)
        now = int(self.clock())
        claims = {name: getattr(registration, name) for name in (*BOUND, *AUDIT)}
        claims.update(schema_version=SCHEMA_VERSION, issued_at=_timestamp(now), expires_at=_timestamp(now + self.ttl))

        def extend(audit: dict) -> dict:
            if audit["state"] == "revoked":
                self.refuse(slug, "unauthenticated", "launch grant was revoked")
            return {**audit, "expires_at": claims["expires_at"], "renewals": audit.get("renewals", 0) + 1}

        self._rewrite(slug, registration.grant_id, extend, "the credential was not renewed")
        return self.sign(claims), claims["expires_at"]

    def revoke(self, slug: str, execution_id: str) -> bool:
        registration = self.registration(slug, execution_id)
        if registration is None:
            return False
        return self._rewrite(slug, registration.grant_id, _revoked, "the registration was not revoked") is not None

    def withdraw(self, slug: str, token: str) -> bool:
        claims = self._verify(slug, token)
        return self._rewrite(slug, claims["grant_id"], _revoked, "the grant was not withdrawn") is not None

    def _audit(self, doc: dict, slug: str, grant_id: str) -> dict:
        return doc["grants"][grant_id]

    def _rewrite(self, slug: str, grant_id: str, change: Callable[[dict], dict | None], failure: str) -> dict | None:
        def rewrite(doc):
            audit = change(self._audit(doc, slug, grant_id))
            if audit is not None:
                doc["grants"][grant_id] = audit
            return audit

        return self._write(slug, rewrite, f"launch grants kept changing; {failure}")

    def disable(self, slug: str) -> list[str]:
        def change(doc):
            revoked = sorted(grant_id for grant_id, audit in doc["grants"].items() if audit["state"] == "issued")
            doc["grants_disabled"] = _timestamp(int(self.clock()))
            for grant_id in revoked:
                doc["grants"][grant_id] = {**doc["grants"][grant_id], "state": "revoked"}
            return revoked

        return self._write(slug, change, "launch grants kept changing; revocation was not committed")

    def enable(self, slug: str) -> None:
        self._write(
            slug, lambda doc: doc.update(grants_disabled=""), "launch grants kept changing; nothing was enabled"
        )


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
