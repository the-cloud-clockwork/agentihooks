import fcntl
import json
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.events.bus import StateConflict
from scripts.events.redis_state import RedisStateStore
from scripts.swarm import keyspace

if TYPE_CHECKING:
    from scripts.events.bus import StateStore

BUCKET = "routing"
ENTRY = "settings"
WRITE_TRIES = 8
LEGACY = f"{keyspace.ROOT}:routing:settings"


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
    **{f"master-account-claude-{slot}": _name for slot in range(2, 6)},
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

    def claim(self, key: str, value: object, actor: str, now: float) -> object:
        _known(key)
        if value is None or not VALIDATORS[key](value):
            raise ValueError(f"invalid value for {key}: {value!r}")
        if not actor:
            raise ValueError("a routing setting write needs an actor")
        return self._claim(key, value, {"key": key, "value": value, "actor": actor, "at": now})

    @abstractmethod
    def history(self) -> list[dict]: ...

    @abstractmethod
    def _claim(self, key: str, value: object, entry: dict) -> object: ...

    @abstractmethod
    def _read(self) -> dict[str, object]: ...

    @abstractmethod
    def _write(self, key: str, value: object, entry: dict) -> None: ...


def _apply(data: dict, key: str, value: object, entry: dict) -> None:
    data["settings"].pop(key, None)
    if value is not None:
        data["settings"][key] = value
    data["history"].append(entry)


def _legacy(client: object) -> dict:
    if client is None:
        return {"settings": {}, "history": []}
    return {
        "settings": {key: json.loads(value) for key, value in client.hgetall(LEGACY).items()},
        "history": [json.loads(row) for row in client.lrange(f"{LEGACY}:history", 0, -1)],
    }


class StateSettings(SettingsStore):
    def __init__(self, state: "StateStore") -> None:
        self.state = state

    def history(self) -> list[dict]:
        return self._load()[0]["history"]

    def _read(self) -> dict[str, object]:
        return self._load()[0]["settings"]

    def _load(self) -> tuple[dict, int | None]:
        entry = self.state.get(ENTRY)
        if entry is None:
            return _legacy(self.state.redis if isinstance(self.state, RedisStateStore) else None), None
        return json.loads(entry.value), entry.revision

    def _commit(self, change: Callable[[dict], object]) -> object:
        for attempt in range(1, WRITE_TRIES + 1):
            data, revision = self._load()
            held = change(data)
            if held is not None:
                return held
            raw = json.dumps(data).encode()
            try:
                if revision is None:
                    self.state.create(ENTRY, raw)
                else:
                    self.state.update(ENTRY, raw, revision)
                return None
            except StateConflict:
                if attempt == WRITE_TRIES:
                    raise

    def _write(self, key: str, value: object, entry: dict) -> None:
        self._commit(lambda data: _apply(data, key, value, entry))

    def _claim(self, key: str, value: object, entry: dict) -> object:
        def change(data: dict) -> object:
            held = data["settings"].get(key)
            if held is None:
                _apply(data, key, value, entry)
            return held

        held = self._commit(change)
        return value if held is None else held


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
        _apply(data, key, value, entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(f"{self.path.name}.tmp")
        temp.write_text(json.dumps(data))
        temp.replace(self.path)

    def _claim(self, key: str, value: object, entry: dict) -> object:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_name(f"{self.path.name}.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            held = self._read().get(key)
            if held is not None:
                return held
            self._write(key, value, entry)
        return value


def open_store(client: "StateStore | None", environ: Mapping[str, str]) -> SettingsStore:
    if client is not None:
        return StateSettings(client)
    home = environ.get("AGENTIHOOKS_HOME") or Path.home() / ".agentihooks"
    return FileSettings(Path(home) / "routing-settings.json")
