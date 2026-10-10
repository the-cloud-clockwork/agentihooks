from __future__ import annotations

import hashlib
import json
import math
import os
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from hooks import config
from hooks.classifier.result import DecisionResult


@dataclass(frozen=True)
class RecordMetadata:
    definition: str | None = None
    definition_digest: str | None = None
    expected: object = None


_METADATA = ContextVar("classifier_record_metadata", default=RecordMetadata())
_UNSET = object()


@contextmanager
def record_context(
    *,
    definition: str | None = None,
    definition_digest: str | None = None,
    expected: object = _UNSET,
) -> Iterator[None]:
    fields = {}
    if definition is not None:
        fields["definition"] = definition
    if definition_digest is not None:
        fields["definition_digest"] = definition_digest
    if expected is not _UNSET:
        fields["expected"] = expected
    token = _METADATA.set(replace(_METADATA.get(), **fields))
    try:
        yield
    finally:
        _METADATA.reset(token)


def log_path() -> Path:
    return config.AGENTIHOOKS_HOME / "classifier" / "decisions.jsonl"


def state_digest(state: object) -> str:
    canonical = json.dumps(state, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def failure_record(model: str, failure: Exception) -> dict:
    record = {"model": model, "reason": str(failure)}
    key = os.environ.get("AGENTIHOOKS_CLASSIFIER_LITELLM_KEY", "")
    return {name: value.replace(key, "[redacted]") if key else value for name, value in record.items()}


def append(
    purpose: str,
    state: object,
    result: DecisionResult | None,
    latency_ms: int,
    failures: list | None = None,
    api_down_cached: bool = False,
) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "purpose": purpose,
        "source": result.source if result else None,
        "calibrated": result.calibrated if result else None,
        "latency_ms": latency_ms,
        "cost": result.cost if result else None,
        "answers": result.to_dict()["answers"] if result else {},
        "state_digest": state_digest(state),
        "failures": failures or [],
        "api_down_cached": api_down_cached,
        **asdict(_METADATA.get()),
    }
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(entry) + "\n")


def read(purpose: str | None = None) -> list:
    path = log_path()
    if not path.exists():
        return []
    entries = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [e for e in entries if purpose is None or e.get("purpose") == purpose]


def _percentile(ordered: list, fraction: float) -> int | None:
    if not ordered:
        return None
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def stats(purpose: str | None = None) -> dict:
    entries = read(purpose)
    answered = [e for e in entries if e.get("source")]
    fallback = [e for e in answered if e.get("calibrated") is not True]
    latencies = sorted(e["latency_ms"] for e in entries)
    return {
        "purpose": purpose,
        "calls": len(entries),
        "unavailable": len(entries) - len(answered),
        "sources": dict(Counter(e["source"] for e in answered)),
        "fallback_rate": round(len(fallback) / len(answered), 4) if answered else None,
        "latency_ms": {name: _percentile(latencies, f) for name, f in (("p50", 0.5), ("p90", 0.9), ("p99", 0.99))},
        "cost": round(sum(e.get("cost") or 0 for e in entries), 9),
    }
