import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

from scripts.swarm_v2 import filesystem

SCHEMA_VERSION = 1
AUTHORITY = ("execution_id", "generation", "task_id", "seat_id", "swarm_id", "grant_id")


class LaunchRefused(ValueError):
    pass


def _command(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or any(not isinstance(v, str) or not v or "\0" in v for v in value):
        raise LaunchRefused("invalid command")
    return tuple(value)


def _deadline(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 < value <= 300
    ):
        raise LaunchRefused("invalid deadline")
    return float(value)


def _authority(value: object) -> dict:
    if not isinstance(value, dict):
        raise LaunchRefused("invalid authority")
    if any(k not in value for k in AUTHORITY):
        raise LaunchRefused("missing authority")
    if not re.fullmatch(r"exe-[0-9a-f]{32}", str(value["execution_id"])):
        raise LaunchRefused("invalid authority")
    if not re.fullmatch(r"lgr-[0-9a-f]{32}", str(value["grant_id"])):
        raise LaunchRefused("invalid authority")
    if type(value["generation"]) is not int or value["generation"] < 1:
        raise LaunchRefused("invalid authority")
    for key in ("task_id", "seat_id", "swarm_id"):
        if not isinstance(value[key], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}", value[key]):
            raise LaunchRefused("invalid authority")
    return value


@dataclass(frozen=True)
class Budgets:
    startup: float = 10
    quiesce: float = 10
    checkpoint: float = 10
    kill: float = 2


@dataclass(frozen=True)
class Launch:
    execution: filesystem.Execution
    authority: dict
    harness: str
    agent: tuple[str, ...]
    exporter: tuple[str, ...] | None
    herdr: tuple[str, ...]
    budgets: Budgets

    @property
    def attempt(self) -> Path:
        return self.execution.root

    @property
    def home(self) -> Path:
        return self.execution.path("home") / self.harness

    @classmethod
    def load(cls, attempt: Path, path: Path) -> "Launch":
        spec = json.loads(path.read_text())
        record = json.loads((attempt / "execution.json").read_text())
        registered = _authority(json.loads((attempt / "registration.json").read_text()))
        if not isinstance(spec, dict) or spec.get("schema_version") != SCHEMA_VERSION:
            raise LaunchRefused("unsupported launch")
        authority = _authority(spec.get("authority"))
        if authority != registered or record["attempt"] != authority["execution_id"]:
            raise LaunchRefused("authority mismatch")
        harness = spec.get("harness")
        if harness not in ("claude", "codex") or harness not in record.get("homes", {}):
            raise LaunchRefused("missing private home")
        execution = filesystem.recorded(attempt.resolve(), record)
        home = (execution.root / record["homes"][harness]).resolve()
        if home != execution.path("home") / harness or not home.is_dir():
            raise LaunchRefused("invalid private home")
        budgets = Budgets(
            *(
                _deadline(spec.get(f"{k}_seconds", default))
                for k, default in (("startup", 10), ("quiesce", 10), ("checkpoint", 10), ("kill", 2))
            )
        )
        return cls(
            execution,
            authority,
            harness,
            _command(spec.get("agent")),
            _command(spec["exporter"]) if "exporter" in spec else None,
            _command(spec.get("herdr", ["herdr", "server"])),
            budgets,
        )
