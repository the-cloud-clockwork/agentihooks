"""The swarm's gate log: one JSON row per deny, would-be deny, lift and fail-open, read by doctor rates and swarm status."""

import json
import math
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

TAIL_BYTES = 65536
DECISIONS = ("deny", "observe", "lift", "fail-open")
UNSAFE = re.compile(r"[^A-Za-z0-9@_-]")


def safe_name(text):
    return UNSAFE.sub("_", text) or "_"


def swarm_home():
    return Path.home() / ".agentihooks" / "swarm"


def gates_dir(slug, home=None):
    return Path(home or swarm_home()) / safe_name(slug) / "gates"


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
    rows = list(_parse(lines[1:] if start else lines))
    return rows if limit is None else rows[-limit:]


def since(slug, since_ms, home=None):
    chunks = []
    try:
        with gate_log_path(slug, home).open("rb") as source:
            for stop in range(source.seek(0, 2), 0, -TAIL_BYTES):
                start = max(0, stop - TAIL_BYTES)
                source.seek(start)
                chunk = source.read(stop - start)
                chunks.append(chunk)
                if _newest(chunk.splitlines()) < since_ms:
                    break
    except OSError:
        return []
    data = b"".join(reversed(chunks))
    return [row for row in _parse(data.splitlines()) if (at := _at(row)) is not None and at >= since_ms]


def _newest(lines):
    return max((at for row in _parse(lines) if (at := _at(row)) is not None), default=math.inf)


def _at(row):
    at = row.get("at")
    return at if isinstance(at, (int, float)) and not isinstance(at, bool) else None


def _parse(lines):
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            yield row


def decisions(slug, limit=20, home=None):
    return [row for row in recent(slug, None, home) if row.get("kind") in DECISIONS][-limit:]
