"""Hive credentials: a one-time invite code is exchanged for a ledger credential and a Redis ACL user."""

import hashlib
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from scripts.swarm.keyspace import ROOT

INVITE_TTL_S = 900
# Outside the ROOT keyspace, so a member's ACL user cannot mint invites or read the credential index.
PREFIX = f"{ROOT}-hive"
ACL_RULES = (f"~{ROOT}:*", f"&{ROOT}:*", "+@all", "-@admin", "-@dangerous")
ENV_FILE = "hive.env"


class HiveError(Exception):
    pass


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def invite(redis, name: str) -> str:
    code = secrets.token_urlsafe(16)
    redis.set(f"{PREFIX}:invite:{_digest(code)}", name, ex=INVITE_TTL_S)
    return code


def exchange(redis, code: str, redis_url: str) -> dict:
    name = redis.getdel(f"{PREFIX}:invite:{_digest(code)}")
    if name is None:
        raise HiveError("invite code is invalid, expired or already used")
    member_id = secrets.token_hex(4)
    password = secrets.token_urlsafe(32)
    ledger = secrets.token_urlsafe(32)
    redis.execute_command("ACL", "SETUSER", f"hive-{member_id}", "reset", "on", f"#{_digest(password)}", *ACL_RULES)
    redis.hset(f"{PREFIX}:member:{member_id}", mapping={"name": name, "ledger": _digest(ledger)})
    redis.set(f"{PREFIX}:ledger:{_digest(ledger)}", member_id)
    return {
        "id": member_id,
        "name": name,
        "ledger_credential": ledger,
        "redis_url": _with_user(redis_url, f"hive-{member_id}", password),
    }


def _with_user(url: str, user: str, password: str) -> str:
    parts = urlsplit(url)
    host = parts.netloc.rpartition("@")[2]
    return urlunsplit(parts._replace(netloc=f"{user}:{password}@{host}"))


def ledger_member(redis, credential: str) -> str | None:
    return redis.get(f"{PREFIX}:ledger:{_digest(credential)}")


def members(redis) -> dict[str, str]:
    return {key.rpartition(":")[2]: redis.hget(key, "name") for key in redis.scan_iter(f"{PREFIX}:member:*")}


def revoke(redis, member_id: str) -> None:
    member = redis.hgetall(f"{PREFIX}:member:{member_id}")
    if not member:
        raise HiveError(f"no hive member {member_id}")
    redis.execute_command("ACL", "DELUSER", f"hive-{member_id}")
    redis.delete(f"{PREFIX}:member:{member_id}", f"{PREFIX}:ledger:{member['ledger']}")


def write_env(home, url: str, grant: dict) -> Path:
    path = Path(home) / ENV_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(
            f"AGENTIHOOKS_HIVE_ID={grant['id']}\n"
            f"AGENTIHOOKS_HIVE_URL={url}\n"
            f"AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL={grant['ledger_credential']}\n"
            f"AGENTIHOOKS_HIVE_REDIS_URL={grant['redis_url']}\n"
        )
    return path
