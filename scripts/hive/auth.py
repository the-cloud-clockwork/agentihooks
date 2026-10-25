"""Hive credentials: a member is minted with a ledger credential and a Redis ACL user."""

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

from scripts.swarm.keyspace import ROOT

if TYPE_CHECKING:
    from redis import Redis

# Outside the ROOT keyspace, so a member's ACL user cannot read the credential index.
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
    "seat:*:recaps",
    "seat:*:learned",
    "seat-of:*:exit",
)
INDEX = f"{ROOT}:hives"
# A Redis started without --aclfile keeps users in memory only; there is nothing to save.
NO_ACL_FILE = "not configured to use an ACL file"
ENV_FILE = "hive.env"
NATS_CREDS_FILE = "hive.creds"
# Below the home, where the hooks' *.env autoload never puts it into other processes.
CONTROLLER_ENV_FILE = "controller/credential.env"
SERVICE_TTL_S = 300


class HiveError(Exception):
    pass


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def mint(redis: "Redis", name: str, redis_url: str) -> dict:
    from redis.exceptions import RedisError

    member_id = secrets.token_hex(8)
    password = secrets.token_urlsafe()
    ledger = secrets.token_urlsafe()
    records = (f"{PREFIX}:member:{member_id}", f"{PREFIX}:ledger:{_digest(ledger)}")
    with redis.pipeline() as pipe:
        pipe.hset(records[0], mapping={"name": name, "ledger": _digest(ledger)})
        pipe.set(records[1], member_id)
        pipe.sadd(INDEX, member_id)
        _set_user(pipe, member_id, password)
        try:
            pipe.execute()
            _persist(redis)
        except RedisError as exc:
            _forget(redis, member_id, records)
            raise HiveError(f"Redis refused the new member ({exc})") from exc
    return {
        "id": member_id,
        "name": name,
        "ledger_credential": ledger,
        "redis_url": _with_user(redis_url, f"hive-{member_id}", password),
    }


def member_of(redis: "Redis", credential: str) -> str | None:
    from redis.exceptions import RedisError

    try:
        return redis.get(f"{PREFIX}:ledger:{_digest(credential)}")
    except RedisError as exc:
        raise HiveError(f"Redis refused the member lookup ({exc})") from exc


def refresh(redis: "Redis", credential: str, redis_url: str) -> dict:
    from redis.exceptions import RedisError

    password = secrets.token_urlsafe()
    ledger_key = f"{PREFIX}:ledger:{_digest(credential)}"

    def recreate(pipe):
        member_id = pipe.get(ledger_key)
        if member_id is None:
            raise HiveError("the ledger credential belongs to no hive member")
        pipe.multi()
        pipe.sadd(INDEX, member_id)
        _set_user(pipe, member_id, password)
        return member_id

    try:
        member_id = redis.transaction(recreate, ledger_key, value_from_callable=True)
        _persist(redis)
    except RedisError as exc:
        raise HiveError(f"Redis refused the refreshed member ({exc})") from exc
    return {"id": member_id, "redis_url": _with_user(redis_url, f"hive-{member_id}", password)}


def _set_user(client, member_id: str, password: str) -> None:
    client.acl_setuser(
        f"hive-{member_id}",
        enabled=True,
        reset=True,
        hashed_passwords=[f"+{_digest(password)}"],
        categories=ACL_CATEGORIES,
        keys=_acl_keys(member_id),
        channels=ACL_PATTERNS,
    )


def _acl_keys(member_id: str) -> tuple[str, ...]:
    own = f"{ROOT}:hive:{member_id}"
    return (f"%R~{ROOT}:*", f"~{own}", f"~{own}:*", *(f"~{ROOT}:{key}" for key in WORKER_KEYS))


def regrant(redis: "Redis") -> None:
    for member_id in redis.smembers(INDEX):
        redis.acl_setuser(
            f"hive-{member_id}",
            enabled=True,
            reset_keys=True,
            reset_channels=True,
            categories=ACL_CATEGORIES,
            keys=_acl_keys(member_id),
            channels=ACL_PATTERNS,
        )
    _persist(redis)


def _persist(redis: "Redis") -> None:
    from redis.exceptions import ResponseError

    try:
        redis.acl_save()
    except ResponseError as exc:
        if NO_ACL_FILE not in str(exc):
            raise


def _forget(redis: "Redis", member_id: str, records: tuple[str, ...]) -> None:
    from redis.exceptions import RedisError

    try:
        redis.delete(*records)
        redis.srem(INDEX, member_id)
        redis.acl_deluser(f"hive-{member_id}")
    except RedisError:
        pass


def _with_user(url: str, user: str, password: str) -> str:
    parts = urlsplit(url)
    host = parts.netloc.rpartition("@")[2]
    return urlunsplit(parts._replace(netloc=f"{user}:{password}@{host}"))


def ledger_member(redis: "Redis", credential: str) -> str | None:
    return redis.get(f"{PREFIX}:ledger:{_digest(credential)}")


def issue_agent(redis: "Redis", member_id: str, slug: str, name: str) -> str:
    token = f"hive.{member_id}.{secrets.token_urlsafe()}"
    key = f"{PREFIX}:member:{member_id}"

    def record(pipe):
        if not pipe.exists(key):
            raise HiveError("Hive membership was revoked")
        pipe.multi()
        pipe.hset(key, f"agent:{_digest(token)}", json.dumps([slug, name]))

    redis.transaction(record, key)
    return token


def agent_member(redis: "Redis", token: str, slug: str, name: str) -> str | None:
    member_id = token.removeprefix("hive.").partition(".")[0]
    scope = redis.hget(f"{PREFIX}:member:{member_id}", f"agent:{_digest(token)}")
    return member_id if scope == json.dumps([slug, name]) else None


def issue_controller(redis: "Redis") -> str:
    credential = secrets.token_urlsafe()
    redis.set(f"{PREFIX}:controller", _digest(credential))
    return credential


def issue_service(redis: "Redis", holder: str) -> str:
    credential = f"{holder}.{secrets.token_urlsafe()}"
    keep_service(redis, credential)
    return credential


def keep_service(redis: "Redis", credential: str) -> None:
    redis.set(f"{PREFIX}:controller:{credential.rpartition('.')[0]}", _digest(credential), ex=SERVICE_TTL_S)


def controller(redis: "Redis", credential: str) -> bool:
    holder, dot, _ = (credential or "").rpartition(".")
    expected = redis.get(f"{PREFIX}:controller:{holder}" if dot else f"{PREFIX}:controller")
    return bool(credential and expected) and hmac.compare_digest(expected, _digest(credential))


def revoke(redis: "Redis", member_id: str) -> None:
    from redis.exceptions import RedisError

    member_key = f"{PREFIX}:member:{member_id}"
    member = redis.hgetall(member_key)
    if not member:
        raise HiveError(f"no hive member {member_id}")
    with redis.pipeline() as pipe:
        pipe.delete(f"{PREFIX}:ledger:{member['ledger']}")
        pipe.srem(INDEX, member_id)
        pipe.acl_deluser(f"hive-{member_id}")
        pipe.execute()
    try:
        _persist(redis)
        redis.delete(member_key)
    except RedisError as exc:
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
