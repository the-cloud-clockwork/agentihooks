"""Operator mode trust for the swarm NATS: keys kept once in the hub signer record, service users signed from a list."""

import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from scripts import openbao_kv
from scripts.events import nkey
from scripts.events.config import CREDENTIALS_FIELD
from scripts.hive import auth, nats_grant

if TYPE_CHECKING:
    from redis import Redis

SIGNER_PATH = f"{nats_grant.HUB_PATH}nats-signer"
SYSTEM_ACCOUNT_NAME = "SYS"
SYSTEM_USER = "system"
SYSTEM_LIMITS = {"subs": -1, "data": -1, "payload": -1, "imports": -1, "exports": -1, "conn": -1, "leaf": -1}
DIRECTIONS = {"publish": "pub", "subscribe": "sub"}
RULES = ("allow", "deny")


def load_users(path: Path | str) -> list[dict]:
    listed = (yaml.safe_load(Path(path).read_text()) or {}).get("users")
    if not isinstance(listed, list) or not listed:
        raise auth.HiveError(f"{path} must list the service users under users")
    for entry in listed:
        if not isinstance(entry, dict) or not entry.get("user") or not entry.get("openbao"):
            raise auth.HiveError(
                f"every user in {path} needs a user name and the openbao record its credential goes to"
            )
    return listed


def load_retired(path: Path | str) -> list[dict]:
    listed = (yaml.safe_load(Path(path).read_text()) or {}).get("retired") or []
    if not isinstance(listed, list) or any(
        not isinstance(entry, dict) or not entry.get("user") or not entry.get("openbao") for entry in listed
    ):
        raise auth.HiveError(f"every retired user in {path} needs a user name and the openbao record to delete")
    signed = {entry["user"] for entry in load_users(path)} & {entry["user"] for entry in listed}
    if signed:
        raise auth.HiveError(f"{path} lists {', '.join(sorted(signed))} under both users and retired")
    return listed


def _subjects(rules) -> dict:
    if not isinstance(rules, dict):
        rules = {"allow": rules}
    return {rule: list(rules[rule]) for rule in RULES if rules.get(rule)}


def service_claims(entry: Mapping) -> dict:
    permissions = entry.get("permissions") or {}
    claims = nats_grant.user_claims()
    for direction, key in DIRECTIONS.items():
        if direction in permissions:
            claims[key] = _subjects(permissions[direction])
    return claims


def service_user(account: nkey.KeyPair, name: str) -> nkey.KeyPair:
    return nkey.derive(account, nkey.USER, f"service-{name}")


def service_creds(account: nkey.KeyPair, entry: Mapping) -> str:
    user = service_user(account, entry["user"])
    return nkey.creds(nkey.encode_jwt(account, user.public, entry["user"], service_claims(entry)), user.seed)


def _keys(bao: openbao_kv.OpenBao, url: str) -> dict[str, nkey.KeyPair]:
    record, _ = openbao_kv.read(bao, SIGNER_PATH)
    if not record:
        keys = {
            "operator": nkey.create(nkey.OPERATOR),
            "system": nkey.create(nkey.ACCOUNT),
            "account": nkey.create(nkey.ACCOUNT),
        }
        system_user = nkey.derive(keys["system"], nkey.USER, SYSTEM_USER)
        system_jwt = nkey.encode_jwt(keys["system"], system_user.public, SYSTEM_USER, nats_grant.user_claims())
        created = {
            **{f"{name}_seed": pair.seed for name, pair in keys.items()},
            "system_credentials": nkey.creds(system_jwt, system_user.seed),
            "url": url,
        }
        if openbao_kv.create(bao, SIGNER_PATH, created):
            return keys
        record, _ = openbao_kv.read(bao, SIGNER_PATH)
    try:
        return {name: nkey.from_seed(record[f"{name}_seed"]) for name in ("operator", "system", "account")}
    except (KeyError, nkey.NkeyError) as exc:
        raise auth.HiveError(f"the NATS signer record {SIGNER_PATH} cannot be used ({exc})") from None


def retire(
    bao: openbao_kv.OpenBao, redis: "Redis", account: nkey.KeyPair, retired: Sequence[Mapping]
) -> tuple[dict[str, int], list[str]]:
    now = int(time.time())
    lines = []
    for entry in retired:
        user = service_user(account, entry["user"]).public
        redis.hsetnx(nats_grant.REVOKED, user, now)
        openbao_kv.delete(bao, entry["openbao"])
        lines.append(
            f"{entry['user']}: revoked {user} in the preloaded swarm account, "
            f"deleted OpenBao {openbao_kv.MOUNT}/{entry['openbao']}"
        )
    return nats_grant.revoked(redis), lines


def trust_values(keys: Mapping[str, nkey.KeyPair], revoked: Mapping[str, int]) -> dict:
    operator, system = keys["operator"], keys["system"]
    operator_claims = {"type": "operator", "version": 2, "system_account": system.public}
    system_claims = {"type": "account", "version": 2, "limits": SYSTEM_LIMITS}
    return {
        "config": {
            "merge": {
                "operator": nkey.encode_jwt(operator, operator.public, "swarm", operator_claims),
                "system_account": system.public,
                "resolver_preload": {
                    system.public: nkey.encode_jwt(operator, system.public, SYSTEM_ACCOUNT_NAME, system_claims),
                    keys["account"].public: nats_grant.account_jwt(operator, keys["account"], revoked),
                },
            }
        }
    }


def bootstrap(
    bao: openbao_kv.OpenBao, redis: "Redis", users: list[dict], url: str, retired: Sequence[Mapping] = ()
) -> tuple[dict, list[str]]:
    keys = _keys(bao, url)
    lines = [f"operator {keys['operator'].public}, swarm account {keys['account'].public}"]
    for entry in users:
        total = openbao_kv.merge(bao, entry["openbao"], {CREDENTIALS_FIELD: service_creds(keys["account"], entry)})
        lines.append(f"{entry['user']}: {openbao_kv.summary(entry['openbao'], [CREDENTIALS_FIELD], total)}")
    revoked, retired_lines = retire(bao, redis, keys["account"], retired)
    return trust_values(keys, revoked), [*lines, *retired_lines]
