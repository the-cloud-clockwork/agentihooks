"""The swarm's gate log: one JSON row per deny, would-be deny, lift and fail-open, read by doctor rates and swarm status."""

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

TAIL_BYTES = 65536


def swarm_home():
    return Path.home() / ".agentihooks" / "swarm"


def gates_dir(slug, home=None):
    return Path(home or swarm_home()) / slug / "gates"


def gate_log_path(slug, home=None):
    return gates_dir(slug, home) / "log.jsonl"


@dataclass(frozen=True)
class Row:
    at: int
    gate: str
    kind: str
    agent: str
    task: str
    tool: str
    reason: str

    @classmethod
    def of(cls, gate, kind, who, tool="", reason="", now_ms=None):
        at = int(time.time() * 1000) if now_ms is None else now_ms
        return cls(at=at, gate=gate, kind=kind, agent=who.name, task=who.task, tool=tool, reason=reason)


def append(slug, row, home=None):
    path = gate_log_path(slug, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as out:
        out.write(json.dumps(asdict(row)) + "\n")


def recent(slug, limit=20, home=None):
    try:
        with gate_log_path(slug, home).open("rb") as source:
            start = max(0, source.seek(0, 2) - TAIL_BYTES)
            source.seek(start)
            lines = source.read().splitlines()
    except OSError:
        return []
    rows = []
    for line in lines[1:] if start else lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows[-limit:]
