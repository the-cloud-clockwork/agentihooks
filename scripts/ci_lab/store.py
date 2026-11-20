"""The lab verdict store: a write once Redis index over the content addressed report and log objects."""

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from scripts.ci_lab.objects import ROOT, Objects, is_report, unit_path

__all__ = ["DISABLED", "INDEX", "PREFIX", "UNFILED", "Store", "Verdict"]

PREFIX = "agentihooks-ci:"
INDEX = f"{PREFIX}verdict:"
DISABLED = f"{PREFIX}disabled"
UNFILED = f"{PREFIX}unfiled"


@dataclass(frozen=True)
class Verdict:
    kind: str
    key: str
    passed: bool
    flaky: bool
    seconds: float

    def __post_init__(self) -> None:
        """Refuse a flaky red: flaky marks a pass after a failure on identical inputs."""
        if self.flaky and not self.passed:
            raise ValueError(f"a flaky verdict is a pass: {self.key}")


def _json(document: Mapping[str, object]) -> str:
    return json.dumps(document, sort_keys=True)


def _stored(report: bytes) -> Verdict:
    return Verdict(**json.loads(report)["verdict"])


class Store:
    """Verdicts by unit key: a write once Redis index over the report and log objects it rebuilds from."""

    def __init__(self, client: Any, objects: Objects) -> None:
        self.client = client
        self.objects = objects

    def get(self, kind: str, key: str, *, bypass_negative: bool = False) -> Verdict | None:
        """Return the stored verdict, or None for an unknown key, a disabled kind or a bypassed negative verdict."""
        if self.client.sismember(DISABLED, kind):
            return None
        raw = self.client.get(f"{INDEX}{kind}:{key}")
        if raw is None:
            return None
        verdict = Verdict(**json.loads(raw))
        return None if bypass_negative and not verdict.passed else verdict

    def put(self, verdict: Verdict, report: Mapping[str, object], log: bytes) -> bool:
        """Store a verdict once; return False when its report exists, indexing that report only where unindexed."""
        report_path = unit_path(verdict.kind, verdict.key, "report")
        log_path = unit_path(verdict.kind, verdict.key, "log")
        document = {"report": dict(report), "verdict": asdict(verdict)}
        written = self.objects.write_once(report_path, _json(document).encode())
        if written:
            self.objects.write_once(log_path, log)
        stored = verdict if written else _stored(self.objects.read(report_path))
        self._index(stored, replace=written)
        return written

    def disable(self, kind: str) -> None:
        """Stop reuse of a kind: every get of it misses until enable."""
        self.client.sadd(DISABLED, kind)

    def enable(self, kind: str) -> None:
        """Resume reuse of a kind."""
        self.client.srem(DISABLED, kind)

    def keep_unfiled(self, kind: str, record: str) -> None:
        """Record a kind's mismatches whose task is not filed yet, replacing the kind's earlier record."""
        self.client.hset(UNFILED, kind, record)

    def unfiled(self) -> dict[str, str]:
        """Return the record of every kind whose mismatch task is not filed yet."""
        return dict(self.client.hgetall(UNFILED))

    def filed(self, kind: str) -> None:
        """Drop a kind's unfiled record once its task is filed."""
        self.client.hdel(UNFILED, kind)

    def reindex(self) -> int:
        """Rebuild the index from the stored report objects; return how many entries it added."""
        reports = [path for path in self.objects.keys(ROOT) if is_report(path)]
        return sum(self._index(_stored(self.objects.read(path)), replace=False) for path in reports)

    def _index(self, verdict: Verdict, *, replace: bool) -> bool:
        key = f"{INDEX}{verdict.kind}:{verdict.key}"
        return bool(self.client.set(key, _json(asdict(verdict)), nx=not replace))
