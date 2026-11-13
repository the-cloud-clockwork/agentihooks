import json
import os
from collections.abc import Sequence
from pathlib import Path
from statistics import mean
from typing import Protocol

from scripts import ci_cpu_quota
from scripts.ci_lab.units import Unit
from scripts.ci_mutation.plan import MUTANT_SECONDS
from tests.queue_shards import SLOWDOWN, _estimates


class DurationHistory(Protocol):
    def durations(self, kind: str, key: str) -> Sequence[float]: ...


def estimate(unit: Unit, durations_path: Path, history: DurationHistory) -> float:
    measured = history.durations(unit.kind, unit.key)
    if measured:
        return mean(measured)
    if unit.kind == "mutant":
        return len(unit.members) * MUTANT_SECONDS
    if unit.kind == "test":
        durations: dict[str, float] = json.loads(durations_path.read_text())
        estimates = _estimates(durations, list(unit.members))
        return sum(estimates.values()) * SLOWDOWN
    raise ValueError(f"No measured duration for {unit.kind}:{unit.key}")


def weight() -> int:
    return ci_cpu_quota.worker_count(os.cpu_count() or 1, ci_cpu_quota.quota_cpus())
