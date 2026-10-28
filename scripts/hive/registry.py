"""Hive registry: one record per hive in the hive state store, indexed there too; <ROOT>:hive:<id> is the legacy hash."""

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path

from scripts.events import state
from scripts.hive import hive_state, scope
from scripts.hive.auth import HiveError
from scripts.swarm.keyspace import ROOT
from scripts.swarm_v2.keyspace import INSTALLATION_FILE

UI_ROLES = ("master", "qa", "frontend")
LIVE_MS = 90_000
HIVE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}")
EXCLUDED_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
JSON_FIELDS = {
    "excluded": list,
    "master_swarms": list,
    "prefer": dict,
    "harnesses": list,
    "slots": list,
    "interactive": dict,
    "master_login": dict,
    "repos": list,
    "capabilities": list,
}
INT_FIELDS = ("max_agents", "heartbeat_at")
LEGACY = "roles"
LEGACY_FIELDS = (LEGACY, "max_agents")
UNSET = ("max_agents", "excluded")
KEPT = (*UNSET, "ui", "ephemeral")
MASTER_NAME = "swarm-master"
INDEX = hive_state.LEGACY_INDEX


def key(hive_id: str) -> str:
    return f"{ROOT}:hive:{hive_id}"


def _default(hive_id: str, ui: str = "no") -> dict:
    headless = ui != "yes"
    return {
        "id": hive_id,
        "name": hive_id,
        "ui": ui,
        "ephemeral": "no",
        "excluded": ["master"] if headless else [],
        "prefer": {},
        "max_agents": 20 if headless else 10,
        "harnesses": [],
        "slots": [],
        "interactive": {"claude": "", "codex": ""},
        "master_login": {"claude": ""},
        "master_login_enabled": "no",
        "repos": [],
        "capabilities": [],
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


def _names(setting: str, value: str, noun: str) -> list[str]:
    names = list(dict.fromkeys(name.strip() for name in value.split(",") if name.strip()))
    for name in names:
        if not EXCLUDED_NAME.fullmatch(name):
            raise HiveError(f"{setting} takes {noun}, not {name!r}")
    return names


def _excluded(setting: str, value: str) -> list[str]:
    return _names(setting, value, "lane roles or profiles")


def _capabilities(setting: str, value: str) -> list[str]:
    return _names(setting, value, "capability names")


def _master_swarms(setting: str, value: str) -> list[str]:
    return _names(setting, value, "swarm slugs")


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
    "draining": ("draining", _flag),
    "excluded": ("excluded", _excluded),
    "master-swarms": ("master_swarms", _master_swarms),
    "prefer": ("prefer", _prefer),
    "max-agents": ("max_agents", _count),
    "capabilities": ("capabilities", _capabilities),
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
    for role in record["prefer"]:
        if role in record["excluded"]:
            raise HiveError(f"prefer names {role}, a role this hive excludes")


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


def _legacy(client, hive_id: str):
    def stored() -> dict | None:
        legacy = client.hgetall(key(hive_id)) if hive_state.on_redis() else {}
        if not legacy:
            return None
        if LEGACY in legacy:
            legacy = {field: value for field, value in legacy.items() if field not in LEGACY_FIELDS}
        return {field: _decode(hive_id, field, value) for field, value in legacy.items()}

    return stored


def _stored(client, hive_id: str) -> dict | None:
    return hive_state.read(hive_state.records(client, hive_id), hive_id, _legacy(client, hive_id))


def _seeded(hive_id: str) -> dict:
    return {field: value for field, value in _default(hive_id).items() if field not in KEPT}


def _amend(client, hive_id: str, change):
    return hive_state.amend(hive_state.records(client, hive_id), hive_id, change, _legacy(client, hive_id))


def _settle(hive_id: str, record: dict) -> dict:
    """The hive hosting the operator's interface runs the masters and is always named MASTER_NAME."""
    settled = {**_default(hive_id, record.get("ui", "no")), **record}
    return {**settled, "name": MASTER_NAME} if settled["ui"] == "yes" else settled


def show(client, hive_id: str) -> dict | None:
    stored = _stored(client, hive_id)
    return None if stored is None else _settle(hive_id, stored)


def update(client, hive_id: str, pairs: list[str], keep: bool = False) -> dict:
    if not HIVE_ID.fullmatch(hive_id):
        raise HiveError(f"hive id {hive_id!r} must be letters, digits, dots, dashes or underscores")
    if not pairs:
        raise HiveError("hive set needs at least one setting")
    parsed = parse(pairs)

    def write(stored: dict | None) -> dict:
        stored = stored or {}
        fields = {f: v for f, v in parsed.items() if not (keep and f in KEPT and f in stored)}
        _check(_settle(hive_id, {**stored, **fields}))
        return {**_seeded(hive_id), **stored, **fields}

    record = _settle(hive_id, _amend(client, hive_id, write))
    if hive_id != scope.member():
        _index(client, hive_id)
    return record


def beat(client, hive_id: str, fields: dict) -> None:
    _amend(client, hive_id, lambda stored: {**_seeded(hive_id), **(stored or {}), **fields})


def forget(client, hive_id: str) -> None:
    _amend(client, hive_id, lambda stored: None)
    hive_state.indexed(client, hive_id, False)


def _index(client, hive_id: str) -> None:
    try:
        hive_state.indexed(client, hive_id, True)
    except state.unavailable():
        pass


def hives(client) -> list[dict]:
    return [record for hive_id in hive_state.index(client) if (record := show(client, hive_id))]


def live(record: dict, now_ms: int) -> bool:
    return record["heartbeat_at"] > 0 and now_ms - record["heartbeat_at"] <= LIVE_MS


def touch(client, hive_id: str, now_ms: int) -> None:
    def refresh(stored: dict | None):
        return {**stored, "heartbeat_at": now_ms} if stored and "heartbeat_at" in stored else hive_state.KEEP

    if "heartbeat_at" in (_amend(client, hive_id, refresh) or {}):
        client.pexpire(f"{key(hive_id)}:sessions", LIVE_MS)


def home(environ: Mapping[str, str] = os.environ) -> Path:
    chosen = environ.get("AGENTIHOOKS_HOME") or os.environ.get("AGENTIHOOKS_HOME")
    return Path(chosen or Path.home() / ".agentihooks")


def seeded_id() -> str:
    try:
        record = json.loads((home() / INSTALLATION_FILE).read_text())
    except (OSError, ValueError):
        return ""
    seeded = record.get("hive_id") if isinstance(record, dict) else None
    return seeded if isinstance(seeded, str) and HIVE_ID.fullmatch(seeded) else ""
