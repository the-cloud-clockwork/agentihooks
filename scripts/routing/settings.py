"""Routing settings: one validated store over a Redis hash, or a JSON file in the agentihooks home without Redis."""

import json
from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path

from scripts.swarm import keyspace


def _weight(value: object) -> bool:
    return type(value) is int and 0 <= value <= 100


def _count(value: object) -> bool:
    return type(value) is int and value >= 0


def _name(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


VALIDATORS = {
    "claude-api-weight": _weight,
    "codex-api-weight": _weight,
    "claude-api-max-sessions": _count,
    "codex-api-max-sessions": _count,
    "master-account-claude": _name,
    "master-account-codex": _name,
    "master-tier-claude": _name,
    "master-tier-codex": _name,
}
DEFAULTS = {"claude-api-weight": 0, "codex-api-weight": 0}


def _known(key: str) -> None:
    if key not in VALIDATORS:
        raise ValueError(f"unknown routing setting {key}")


class SettingsStore(ABC):
    def all(self) -> dict[str, object]:
        return DEFAULTS | self._read()

    def get(self, key: str) -> object:
        _known(key)
        return self.all().get(key)

    def set(self, key: str, value: object, actor: str, now: float) -> None:
        _known(key)
        if value is not None and not VALIDATORS[key](value):
            raise ValueError(f"invalid value for {key}: {value!r}")
        if not actor:
            raise ValueError("a routing setting write needs an actor")
        self._write(key, value, {"key": key, "value": value, "actor": actor, "at": now})

    @abstractmethod
    def history(self) -> list[dict]: ...

    @abstractmethod
    def _read(self) -> dict[str, object]: ...

    @abstractmethod
    def _write(self, key: str, value: object, entry: dict) -> None: ...


class RedisSettings(SettingsStore):
    def __init__(self, client) -> None:
        self.client = client
        self.key = f"{keyspace.ROOT}:routing:settings"
        self.history_key = f"{self.key}:history"

    def history(self) -> list[dict]:
        return [json.loads(entry) for entry in self.client.lrange(self.history_key, 0, -1)]

    def _read(self) -> dict[str, object]:
        return {key: json.loads(value) for key, value in self.client.hgetall(self.key).items()}

    def _write(self, key: str, value: object, entry: dict) -> None:
        pipe = self.client.pipeline()
        if value is None:
            pipe.hdel(self.key, key)
        else:
            pipe.hset(self.key, key, json.dumps(value))
        pipe.rpush(self.history_key, json.dumps(entry))
        pipe.execute()


class FileSettings(SettingsStore):
    def __init__(self, path: Path) -> None:
        self.path = path

    def history(self) -> list[dict]:
        return self._load()["history"]

    def _read(self) -> dict[str, object]:
        return self._load()["settings"]

    def _load(self) -> dict:
        if not self.path.exists():
            return {"settings": {}, "history": []}
        return json.loads(self.path.read_text())

    def _write(self, key: str, value: object, entry: dict) -> None:
        data = self._load()
        data["settings"].pop(key, None)
        if value is not None:
            data["settings"][key] = value
        data["history"].append(entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data))


def open_store(client, environ: Mapping[str, str]) -> SettingsStore:
    if client is not None:
        return RedisSettings(client)
    home = environ.get("AGENTIHOOKS_HOME") or Path.home() / ".agentihooks"
    return FileSettings(Path(home) / "routing-settings.json")
