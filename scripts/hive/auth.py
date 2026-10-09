"""Hive credentials: a one-time invite code is exchanged for a ledger credential and a Redis ACL user."""

import hashlib
import hmac
import os
import secrets
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

from scripts.swarm.keyspace import ROOT

if TYPE_CHECKING:
    from redis import Redis

INVITE_TTL_S = 900
# Outside the ROOT keyspace, so a member's ACL user cannot mint invites or read the credential index.
PREFIX = f"{ROOT}-hive"
# ACL cannot confine a user to one database, so a hive's Redis serves that hive alone.
ACL_PATTERNS = (f"{ROOT}:*",)
ACL_CATEGORIES = ("+@all", "-@admin", "-@dangerous")
ENV_FILE = "hive.env"
# Below the home, where the hooks' *.env autoload never puts it into other processes.
CONTROLLER_ENV_FILE = "controller/credential.env"


class HiveError(Exception):
    pass


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def invite(redis: "Redis", name: str) -> str:
    code = secrets.token_hex(16)
    redis.set(f"{PREFIX}:invite:{_digest(code)}", name, ex=INVITE_TTL_S)
    return code


def exchange(redis: "Redis", code: str, redis_url: str) -> dict:
    from redis.exceptions import RedisError

    name = redis.getdel(f"{PREFIX}:invite:{_digest(code)}")
    if name is None:
        raise HiveError("invite code is invalid, expired or already used")
    member_id = secrets.token_hex(8)
    password = secrets.token_urlsafe()
    ledger = secrets.token_urlsafe()
    records = (f"{PREFIX}:member:{member_id}", f"{PREFIX}:ledger:{_digest(ledger)}")
    with redis.pipeline() as pipe:
        pipe.hset(records[0], mapping={"name": name, "ledger": _digest(ledger)})
        pipe.set(records[1], member_id)
        pipe.acl_setuser(
            f"hive-{member_id}",
            enabled=True,
            hashed_passwords=[f"+{_digest(password)}"],
            categories=ACL_CATEGORIES,
            keys=ACL_PATTERNS,
            channels=ACL_PATTERNS,
        )
        try:
            pipe.execute()
        except RedisError as exc:
            _forget(redis, member_id, records)
            raise HiveError(f"Redis refused the new member ({exc})") from exc
    return {
        "id": member_id,
        "name": name,
        "ledger_credential": ledger,
        "redis_url": _with_user(redis_url, f"hive-{member_id}", password),
    }


def _forget(redis: "Redis", member_id: str, records: tuple[str, ...]) -> None:
    from redis.exceptions import RedisError

    try:
        redis.delete(*records)
        redis.acl_deluser(f"hive-{member_id}")
    except RedisError:
        pass


def _with_user(url: str, user: str, password: str) -> str:
    parts = urlsplit(url)
    host = parts.netloc.rpartition("@")[2]
    return urlunsplit(parts._replace(netloc=f"{user}:{password}@{host}"))


def ledger_member(redis: "Redis", credential: str) -> str | None:
    return redis.get(f"{PREFIX}:ledger:{_digest(credential)}")


def issue_controller(redis: "Redis") -> str:
    credential = secrets.token_urlsafe()
    redis.set(f"{PREFIX}:controller", _digest(credential))
    return credential


def controller(redis: "Redis", credential: str) -> bool:
    expected = redis.get(f"{PREFIX}:controller")
    return bool(credential and expected) and hmac.compare_digest(expected, _digest(credential))


def revoke(redis: "Redis", member_id: str) -> None:
    member = redis.hgetall(f"{PREFIX}:member:{member_id}")
    if not member:
        raise HiveError(f"no hive member {member_id}")
    with redis.pipeline() as pipe:
        pipe.delete(f"{PREFIX}:member:{member_id}", f"{PREFIX}:ledger:{member['ledger']}")
        pipe.acl_deluser(f"hive-{member_id}")
        pipe.execute()


def write_env(home: Path | str, url: str, grant: dict) -> Path:
    return _write_private(
        Path(home) / ENV_FILE,
        f"AGENTIHOOKS_HIVE_ID={grant['id']}\n"
        f"AGENTIHOOKS_HIVE_URL={url}\n"
        f"AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL={grant['ledger_credential']}\n"
        f"AGENTIHOOKS_HIVE_REDIS_URL={grant['redis_url']}\n",
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
