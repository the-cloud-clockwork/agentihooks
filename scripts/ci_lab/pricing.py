import json
import os
from collections.abc import Sequence
from pathlib import Path
from statistics import mean
from typing import Protocol

from scripts import ci_cpu_quota
from scripts.ci_lab.units import Unit
from scripts.ci_mutation.plan import MUTANT_SECONDS
from tests.queue_shards import SLOWDOWN


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
        return test_estimate(unit.members, durations)
    raise ValueError(f"No measured duration for {unit.kind}:{unit.key}")


def test_estimate(members: tuple[str, ...], durations: dict[str, float]) -> float:
    values = sorted(durations.values())
    fallback = values[(len(values) * 99 - 1) // 100]
    per_file: dict[str, float] = {}
    for node, seconds in durations.items():
        path = node.partition("::")[0]
        per_file[path] = max(seconds, per_file.get(path, 0.0))
    return sum(durations.get(node, per_file.get(node.partition("::")[0], fallback)) for node in members) * SLOWDOWN


def weight() -> int:
    return ci_cpu_quota.worker_count(os.cpu_count() or 1, ci_cpu_quota.quota_cpus())
