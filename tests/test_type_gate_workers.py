from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from scripts import type_gate


def _measure_workers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    source = tmp_path / "scripts" / "one.py"
    source.parent.mkdir()
    source.write_text("value = 1\n")
    limits: list[int] = []

    def pool(max_workers: int) -> ThreadPoolExecutor:
        limits.append(max_workers)
        return ThreadPoolExecutor(max_workers)

    def check(head: Path, files: list[str]) -> list[dict[str, Any]]:
        assert head == tmp_path
        assert files == ["scripts/one.py"]
        return []

    monkeypatch.setattr(type_gate, "ThreadPoolExecutor", pool)
    monkeypatch.setattr(type_gate, "_check", check)
    assert type_gate.measure(tmp_path) == {}
    assert len(limits) == 1
    return limits[0]


@pytest.mark.feature("type-gate-cpu-workers")
@pytest.mark.parametrize(
    ("cpus", "load", "expected"),
    [(1, 2.0, 1), (16, 0.0, 4), (4, 3.0, 1), (4, 1.5, 2)],
    ids=["loaded-single-cpu", "idle-sixteen-cpus", "loaded-four-cpus", "fractional-free-cpus"],
)
def test_measure_sizes_workers_from_allowed_cpus_and_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cpus: int, load: float, expected: int
) -> None:
    monkeypatch.delenv("AGENTIHOOKS_TYPE_WORKERS", raising=False)
    monkeypatch.setattr(type_gate.os, "sched_getaffinity", lambda _pid: set(range(cpus)), raising=False)
    monkeypatch.setattr(type_gate.os, "getloadavg", lambda: (load, 0.0, 0.0), raising=False)
    assert _measure_workers(tmp_path, monkeypatch) == expected
