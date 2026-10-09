"""Hive registry: one Redis hash per hive under <ROOT>:hive:<id>, indexed in <ROOT>:hives."""

import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.hive.auth import HiveError
from scripts.swarm.keyspace import ROOT
from scripts.swarm_v2.keyspace import INSTALLATION_FILE

if TYPE_CHECKING:
    from redis import Redis
    from redis.client import Pipeline

ROLES = ("master", "qa", "frontend", "eng", "ci", "plan")
UI_ROLES = ("master", "qa", "frontend")
LIVE_MS = 90_000
INDEX = f"{ROOT}:hives"
HIVE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}")
JSON_FIELDS = {"roles": list, "prefer": dict, "harnesses": list, "slots": list, "interactive": dict, "repos": list}
INT_FIELDS = ("max_agents", "heartbeat_at")


def key(hive_id: str) -> str:
    return f"{ROOT}:hive:{hive_id}"


def _default(hive_id: str) -> dict:
    return {
        "id": hive_id,
        "name": hive_id,
        "ui": "no",
        "ephemeral": "no",
        "roles": [],
        "prefer": {},
        "max_agents": 1,
        "harnesses": [],
        "slots": [],
        "interactive": {"claude": "", "codex": ""},
        "repos": [],
        "heartbeat_at": 0,
        "version": "",
    }


def _name(setting: str, value: str) -> str:
    if not value:
        raise HiveError(f"{setting} must not be empty")
    return value


def _flag(setting: str, value: str) -> str:
    if value not in ("yes", "no"):
        raise HiveError(f"{setting} must be yes or no, not {value!r}")
    return value


def _count(setting: str, value: str) -> int:
    if not (value.isascii() and value.isdigit()) or int(value) < 1:
        raise HiveError(f"{setting} must be a positive whole number, not {value!r}")
    return int(value)


def _roles(setting: str, value: str) -> list[str]:
    roles = list(dict.fromkeys(role for role in value.split(",") if role))
    for role in roles:
        if role not in ROLES:
            raise HiveError(f"unknown role {role}; roles are {', '.join(ROLES)}")
    return roles


def _prefer(setting: str, value: str) -> dict[str, int]:
    prefer = {}
    for pair in filter(None, value.split(",")):
        role, colon, rank = pair.partition(":")
        if not colon:
            raise HiveError(f"{setting} takes role:rank, not {pair!r}")
        prefer[role] = _count(f"the rank of {role}", rank)
    return prefer


SETTINGS = {
    "name": ("name", _name),
    "ui": ("ui", _flag),
    "ephemeral": ("ephemeral", _flag),
    "roles": ("roles", _roles),
    "prefer": ("prefer", _prefer),
    "max-agents": ("max_agents", _count),
}


def parse(pairs: list[str]) -> dict:
    fields = {}
    for pair in pairs:
        setting, equals, value = pair.partition("=")
        if not equals or setting not in SETTINGS:
            raise HiveError(f"unknown setting {pair!r}; settings are {', '.join(SETTINGS)}")
        field, read = SETTINGS[setting]
        fields[field] = read(setting, value)
    return fields


def _check(record: dict) -> None:
    for role in record["roles"]:
        if role in UI_ROLES and record["ui"] == "no":
            raise HiveError(f"role {role} needs a UI, and this hive has ui=no")
    for role in record["prefer"]:
        if role not in record["roles"]:
            raise HiveError(f"prefer names {role}, a role this hive does not take")


def _encode(field: str, value: object) -> str:
    return json.dumps(value) if field in JSON_FIELDS else str(value)


def _decode(hive_id: str, field: str, value: str) -> object:
    try:
        if field in JSON_FIELDS:
            decoded = json.loads(value)
            if not isinstance(decoded, JSON_FIELDS[field]):
                raise ValueError
            return decoded
        return int(value) if field in INT_FIELDS else value
    except ValueError as exc:
        raise HiveError(f"hive {hive_id} holds an unreadable {field}: {value!r}") from exc


def show(redis: "Redis", hive_id: str) -> dict | None:
    stored = redis.hgetall(key(hive_id))
    if not stored:
        return None
    return {**_default(hive_id), **{field: _decode(hive_id, field, value) for field, value in stored.items()}}


def update(redis: "Redis", hive_id: str, pairs: list[str]) -> dict:
    if not HIVE_ID.fullmatch(hive_id):
        raise HiveError(f"hive id {hive_id!r} must be letters, digits, dots, dashes or underscores")
    if not pairs:
        raise HiveError("hive set needs at least one setting")
    fields = parse(pairs)

    def write(pipe: "Pipeline") -> dict:
        record = {**(show(pipe, hive_id) or _default(hive_id)), **fields}
        _check(record)
        pipe.multi()
        for field, value in _default(hive_id).items():
            pipe.hsetnx(key(hive_id), field, _encode(field, value))
        pipe.hset(key(hive_id), mapping={field: _encode(field, value) for field, value in fields.items()})
        pipe.sadd(INDEX, hive_id)
        return record

    return redis.transaction(write, key(hive_id), value_from_callable=True)


def hives(redis: "Redis") -> list[dict]:
    return [record for hive_id in sorted(redis.smembers(INDEX)) if (record := show(redis, hive_id))]


def live(record: dict, now_ms: int) -> bool:
    return record["heartbeat_at"] > 0 and now_ms - record["heartbeat_at"] <= LIVE_MS


def home() -> Path:
    return Path(os.environ.get("AGENTIHOOKS_HOME") or Path.home() / ".agentihooks")


def seeded_id() -> str:
    try:
        record = json.loads((home() / INSTALLATION_FILE).read_text())
    except (OSError, ValueError):
        return ""
    seeded = record.get("hive_id") if isinstance(record, dict) else None
    return seeded if isinstance(seeded, str) and HIVE_ID.fullmatch(seeded) else ""
