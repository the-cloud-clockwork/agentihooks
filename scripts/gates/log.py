"""The swarm's gate log: one JSON row per deny, would-be deny, lift and fail-open, read by doctor rates and swarm status."""

import json
import math
import re
import time
from collections.abc import Generator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from scripts.gates.base import Who

TAIL_BYTES = 65536
DECISIONS = ("deny", "observe", "lift", "fail-open")
UNSAFE = re.compile(r"[^A-Za-z0-9@_-]")


def safe_name(text: str) -> str:
    return UNSAFE.sub("_", text) or "_"


def swarm_home() -> Path:
    return Path.home() / ".agentihooks" / "swarm"


def gates_dir(slug: str, home: str | Path | None = None) -> Path:
    return Path(home or swarm_home()) / safe_name(slug) / "gates"


def gate_log_path(slug: str, home: str | Path | None = None) -> Path:
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
    def of(
        cls,
        gate: str,
        kind: str,
        who: "Who",
        tool: str = "",
        reason: str = "",
        now_ms: int | None = None,
    ) -> "Row":
        at = int(time.time() * 1000) if now_ms is None else now_ms
        return cls(at=at, gate=gate, kind=kind, agent=who.name, task=who.task, tool=tool, reason=reason)


def append(slug: str, row: Row, home: str | Path | None = None) -> None:
    path = gate_log_path(slug, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as out:
        out.write(json.dumps(asdict(row)) + "\n")


def recent(slug: str, limit: int | None = 20, home: str | Path | None = None) -> list[dict[str, Any]]:
    try:
        with gate_log_path(slug, home).open("rb") as source:
            start = max(0, source.seek(0, 2) - TAIL_BYTES)
            source.seek(start)
            lines = source.read().splitlines()
    except OSError:
        return []
    rows = list(_parse(lines[1:] if start else lines))
    return rows if limit is None else rows[-limit:]


def since(slug: str, since_ms: int, home: str | Path | None = None) -> list[dict[str, Any]]:
    chunks: list[bytes] = []
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


def _newest(lines: list[bytes]) -> float:
    return max((at for row in _parse(lines) if (at := _at(row)) is not None), default=math.inf)


def _at(row: dict[str, Any]) -> int | float | None:
    at = row.get("at")
    return at if isinstance(at, (int, float)) and not isinstance(at, bool) else None


def _parse(lines: list[bytes]) -> "Generator[dict[str, Any], Any, None]":
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            yield row


def decisions(slug: str, limit: int = 20, home: str | Path | None = None) -> list[dict[str, Any]]:
    return [row for row in recent(slug, None, home) if row.get("kind") in DECISIONS][-limit:]
