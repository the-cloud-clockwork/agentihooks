"""Hive credentials: a member is minted with a ledger credential in the hive state store and a Redis ACL user."""

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from scripts.events import state
from scripts.hive import hive_state, redis_acl
from scripts.swarm.keyspace import ROOT

# Outside the ROOT keyspace, so a member's ACL user cannot read the credential store.
PREFIX = f"{ROOT}-hive"
# ACL cannot confine a user to one database, so a hive's Redis serves that hive alone.
ACL_PATTERNS = (f"{ROOT}:*",)
ACL_CATEGORIES = ("+@all", "-@admin", "-@dangerous")
# Only keys a traced worker command writes; an entry with no writer widens every member's login.
WORKER_KEYS = (
    "swarm:*:agents",
    "swarm:*:executions",
    "swarm:*:execution-publishing",
    "swarm:*:codex-transport",
    "swarm:*:codex-endpoints",
    "swarm:*:codex-openings",
    "swarm:*:codex-viewers",
    "swarm:*:identity-conflicts",
    "swarm:*:desired-routes",
    "swarm:*:state-since",
    "swarm:*:claim:*",
    "swarm:*:ready",
    "swarm:*:restart-causes",
    "swarm:*:transfers",
    "swarm:*:handoff:*",
    "swarm:*:handoff-seat:*",
    "swarm:*:handoff-envelope:*",
    "swarm:*:findings",
    "swarm:*:checks:*",
    "swarm:*:merge-stall",
    "swarm:*:merge-stall-notice",
    "swarm:*:hive-placement-waits",
    "swarm:*:dev-red",
    "swarm:*:prerequisite-waits",
    "swarm:*:stage-covers",
    "seat:*:recaps",
    "seat:*:learned",
    "seat-of:*:exit",
)
INDEX = hive_state.LEGACY_INDEX
ENV_FILE = "hive.env"
NATS_CREDS_FILE = "hive.creds"
# Below the home, where the hooks' *.env autoload never puts it into other processes.
CONTROLLER_ENV_FILE = "controller/credential.env"
SERVICE_TTL_S = 300


class HiveError(Exception):
    pass


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _store(client):
    return hive_state.credentials(client, PREFIX)


def _legacy(client, read):
    return lambda: read(client) if hive_state.on_redis() else None


def _legacy_member(member_id: str):
    def read(client) -> dict | None:
        stored = client.hgetall(f"{PREFIX}:member:{member_id}")
        if not stored:
            return None
        agents = {
            field.removeprefix("agent:"): json.loads(value)
            for field, value in stored.items()
            if field.startswith("agent:")
        }
        return {"name": stored.get("name", ""), "ledger": stored.get("ledger", ""), "agents": agents}

    return read


def _refusals() -> tuple[type[BaseException], ...]:
    return (*state.unavailable(), redis_acl.Refused)


def _refused(exc: BaseException, what: str) -> HiveError:
    source = "Redis" if isinstance(exc, redis_acl.Refused) else "the hive state store"
    return HiveError(f"{source} refused {what} ({exc})")


def member(client, member_id: str) -> dict | None:
    return hive_state.read(_store(client), f"member.{member_id}", _legacy(client, _legacy_member(member_id)))


def members(client) -> dict[str, dict]:
    return {member_id: found for member_id in hive_state.index(client) if (found := member(client, member_id))}


def mint(redis, name: str, redis_url: str) -> dict:
    member_id = secrets.token_hex(8)
    password = secrets.token_urlsafe()
    ledger = secrets.token_urlsafe()
    digest = _digest(ledger)
    store = _store(redis)
    try:
        hive_state.amend(store, f"member.{member_id}", lambda _: {"name": name, "ledger": digest, "agents": {}})
        hive_state.amend(store, f"ledger.{digest}", lambda _: member_id)
        hive_state.indexed(redis, member_id, True)
        _set_user(redis, member_id, password)
        redis_acl.save(redis)
    except _refusals() as exc:
        _forget(redis, member_id, digest)
        raise _refused(exc, "the new member") from exc
    return {
        "id": member_id,
        "name": name,
        "ledger_credential": ledger,
        "redis_url": _with_user(redis_url, f"hive-{member_id}", password),
    }


def member_of(redis, credential: str) -> str | None:
    try:
        return ledger_member(redis, credential)
    except state.unavailable() as exc:
        raise _refused(exc, "the member lookup") from exc


def refresh(redis, credential: str, redis_url: str) -> dict:
    password = secrets.token_urlsafe()
    try:
        member_id = ledger_member(redis, credential)
        if member_id is None:
            raise HiveError("the ledger credential belongs to no hive member")
        hive_state.indexed(redis, member_id, True)
        _set_user(redis, member_id, password)
        redis_acl.save(redis)
    except _refusals() as exc:
        raise _refused(exc, "the refreshed member") from exc
    return {"id": member_id, "redis_url": _with_user(redis_url, f"hive-{member_id}", password)}


def _set_user(client, member_id: str, password: str) -> None:
    redis_acl.set_user(
        client, f"hive-{member_id}", _digest(password), ACL_CATEGORIES, _acl_keys(member_id), ACL_PATTERNS
    )


def _acl_keys(member_id: str) -> tuple[str, ...]:
    own = f"{ROOT}:hive:{member_id}"
    record = f"{ROOT}:swarm:state:hive-{member_id}:*"
    return (f"%R~{ROOT}:*", f"~{own}", f"~{own}:*", f"~{record}", *(f"~{ROOT}:{key}" for key in WORKER_KEYS))


def regrant(redis) -> None:
    for member_id in hive_state.index(redis):
        redis_acl.regrant(redis, f"hive-{member_id}", ACL_CATEGORIES, _acl_keys(member_id), ACL_PATTERNS)
    redis_acl.save(redis)


def _forget(redis, member_id: str, digest: str) -> None:
    store = _store(redis)
    try:
        hive_state.amend(store, f"ledger.{digest}", lambda _: None)
        hive_state.amend(store, f"member.{member_id}", lambda _: None)
        hive_state.indexed(redis, member_id, False)
        redis_acl.drop(redis, f"hive-{member_id}")
    except _refusals():
        pass


def _with_user(url: str, user: str, password: str) -> str:
    parts = urlsplit(url)
    host = parts.netloc.rpartition("@")[2]
    return urlunsplit(parts._replace(netloc=f"{user}:{password}@{host}"))


def ledger_member(redis, credential: str) -> str | None:
    digest = _digest(credential)
    return hive_state.read(
        _store(redis), f"ledger.{digest}", _legacy(redis, lambda c: c.get(f"{PREFIX}:ledger:{digest}"))
    )


def issue_agent(redis, member_id: str, slug: str, name: str) -> str:
    token = f"hive.{member_id}.{secrets.token_urlsafe()}"

    def record(current: dict | None) -> dict:
        if current is None:
            raise HiveError("Hive membership was revoked")
        return {**current, "agents": {**current.get("agents", {}), _digest(token): [slug, name]}}

    hive_state.amend(_store(redis), f"member.{member_id}", record, _legacy(redis, _legacy_member(member_id)))
    return token


def agent_member(redis, token: str, slug: str, name: str) -> str | None:
    member_id = token.removeprefix("hive.").partition(".")[0]
    scope = (member(redis, member_id) or {}).get("agents", {}).get(_digest(token))
    return member_id if scope == [slug, name] else None


def issue_controller(redis) -> str:
    credential = secrets.token_urlsafe()
    hive_state.amend(_store(redis), "controller", lambda _: _digest(credential))
    return credential


def issue_service(redis, holder: str) -> str:
    credential = f"{holder}.{secrets.token_urlsafe()}"
    keep_service(redis, credential)
    return credential


def _service_key(holder: str) -> str:
    return f"service.{_digest(holder)}"


def keep_service(redis, credential: str) -> None:
    held = {"digest": _digest(credential), "expires": time.time() + SERVICE_TTL_S}
    hive_state.amend(_store(redis), _service_key(credential.rpartition(".")[0]), lambda _: held)


def _expected(redis, holder: str, dot: str) -> str | None:
    if not dot:
        return hive_state.read(_store(redis), "controller", _legacy(redis, lambda c: c.get(f"{PREFIX}:controller")))
    legacy = _legacy(redis, lambda c: (held := c.get(f"{PREFIX}:controller:{holder}")) and {"digest": held})
    service = hive_state.read(_store(redis), _service_key(holder), legacy)
    return service["digest"] if service and service.get("expires", float("inf")) > time.time() else None


def controller(redis, credential: str) -> bool:
    if not credential:
        return False
    holder, dot, _ = credential.rpartition(".")
    expected = _expected(redis, holder, dot)
    return bool(expected) and hmac.compare_digest(expected, _digest(credential))


def revoke(redis, member_id: str) -> None:
    found = member(redis, member_id)
    if not found:
        raise HiveError(f"no hive member {member_id}")
    store = _store(redis)
    try:
        hive_state.amend(store, f"ledger.{found['ledger']}", lambda _: None)
        hive_state.indexed(redis, member_id, False)
        redis_acl.drop(redis, f"hive-{member_id}")
        redis_acl.save(redis)
        hive_state.amend(store, f"member.{member_id}", lambda _: None)
    except _refusals() as exc:
        raise HiveError(
            f"revoking {member_id} is incomplete and a Redis restart before it completes revives its Redis user; "
            f"run revoke again ({exc})"
        ) from exc


def write_env(home: Path | str, url: str, grant: dict) -> Path:
    creds = Path(home) / NATS_CREDS_FILE
    if grant.get("nats_credentials"):
        grant = {**grant, "bus_credentials": str(_write_private(creds, grant["nats_credentials"]))}
    else:
        creds.unlink(missing_ok=True)
    return _write_private(Path(home) / ENV_FILE, env_text(url, grant))


def env_text(url: str, grant: dict) -> str:
    bus = (
        f"AGENTIHOOKS_BUS_CREDENTIALS={grant['bus_credentials']}\nAGENTIHOOKS_BUS_BIND=1\n"
        if grant.get("bus_credentials")
        else ""
    )
    if grant.get("bus_url"):
        bus += f"AGENTIHOOKS_BUS=nats\nAGENTIHOOKS_BUS_URL={grant['bus_url']}\n"
    if grant.get("bus_scoped"):
        bus += "AGENTIHOOKS_BUS_SCOPED=1\n"
    if grant.get("bus_swarms"):
        bus += f"AGENTIHOOKS_BUS_SWARMS={','.join(grant['bus_swarms'])}\n"
    return (
        f"AGENTIHOOKS_HIVE_ID={grant['id']}\n"
        f"AGENTIHOOKS_HIVE_URL={url}\n"
        f"AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL={grant['ledger_credential']}\n"
        f"AGENTIHOOKS_HIVE_REDIS_URL={grant['redis_url']}\n"
        f"{bus}"
    )


def write_controller_env(home: Path | str, credential: str) -> Path:
    return _write_private(Path(home) / CONTROLLER_ENV_FILE, f"AGENTIHOOKS_CONTROLLER_CREDENTIAL={credential}\n")


def _write_private(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    return path
