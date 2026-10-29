import json
import time
import uuid
from dataclasses import asdict, dataclass, fields

SCHEMA_VERSION = 1

_TEXT = ("id", "type", "swarm", "entity", "key", "author")
_COUNTS = ("version", "time")


def _count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


@dataclass(frozen=True)
class Envelope:
    id: str
    type: str
    swarm: str
    entity: str
    key: str
    version: int
    time: int
    author: str
    data: dict
    cause: str | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported event schema version {self.schema_version}")
        for name in _TEXT:
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"event {name} must be non empty text")
        for name in _COUNTS:
            if not _count(getattr(self, name)):
                raise ValueError(f"event {name} must be a non negative integer")
        if self.cause is not None and (not isinstance(self.cause, str) or not self.cause):
            raise ValueError("event cause must be non empty text or None")
        if not isinstance(self.data, dict):
            raise ValueError("event data must be a dict")

    def to_dict(self) -> dict:
        return asdict(self)

    def encode(self) -> bytes:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode()

    @classmethod
    def from_dict(cls, raw: dict) -> "Envelope":
        if not isinstance(raw, dict):
            raise ValueError("event envelope must be a JSON object")
        names = {field.name for field in fields(cls)}
        missing = sorted(names - raw.keys())
        unknown = sorted(raw.keys() - names)
        if missing or unknown:
            parts = [f"missing {' '.join(missing)}"] if missing else []
            parts += [f"unknown {' '.join(unknown)}"] if unknown else []
            raise ValueError(f"event envelope fields differ: {', '.join(parts)}")
        return cls(**raw)

    @classmethod
    def decode(cls, raw: bytes) -> "Envelope":
        return cls.from_dict(json.loads(raw))


def create(**values) -> Envelope:
    return Envelope(id=uuid.uuid4().hex, time=time.time_ns() // 1_000_000, **values)
