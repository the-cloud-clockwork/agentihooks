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

    def affinity(pid: int) -> set[int]:
        assert pid == 0
        return set(range(cpus))

    monkeypatch.setattr(type_gate.os, "sched_getaffinity", affinity, raising=False)
    monkeypatch.setattr(type_gate.os, "getloadavg", lambda: (load, 0.0, 0.0), raising=False)
    assert _measure_workers(tmp_path, monkeypatch) == expected


@pytest.mark.feature("type-gate-cpu-workers")
@pytest.mark.parametrize(("setting", "expected"), [("1", 1), ("3", 3), ("4", 4), ("99", 4), ("0", 1), ("-8", 1)])
def test_measure_honors_a_bounded_worker_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setting: str, expected: int
) -> None:
    monkeypatch.setenv("AGENTIHOOKS_TYPE_WORKERS", setting)

    def unavailable(_pid: int) -> set[int]:
        raise AssertionError("an explicit worker override must bypass host sampling")

    monkeypatch.setattr(type_gate.os, "sched_getaffinity", unavailable, raising=False)
    assert _measure_workers(tmp_path, monkeypatch) == expected


@pytest.mark.feature("type-gate-cpu-workers")
@pytest.mark.parametrize("setting", ["", "invalid", "1.5"])
def test_measure_refuses_an_invalid_worker_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setting: str
) -> None:
    monkeypatch.setenv("AGENTIHOOKS_TYPE_WORKERS", setting)
    with pytest.raises(type_gate.GradeError) as error:
        _measure_workers(tmp_path, monkeypatch)
    assert str(error.value) == "AGENTIHOOKS_TYPE_WORKERS must be an integer"


@pytest.mark.feature("type-gate-cpu-workers")
@pytest.mark.parametrize("affinity_missing", [True, False], ids=["missing-affinity", "failed-affinity"])
@pytest.mark.parametrize(("cpus", "expected"), [(None, 1), (1, 1), (8, 4)])
def test_measure_uses_cpu_count_when_affinity_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, affinity_missing: bool, cpus: int | None, expected: int
) -> None:
    monkeypatch.delenv("AGENTIHOOKS_TYPE_WORKERS", raising=False)

    def unavailable(_pid: int) -> set[int]:
        raise OSError("CPU affinity unavailable")

    if affinity_missing:
        monkeypatch.delattr(type_gate.os, "sched_getaffinity", raising=False)
    else:
        monkeypatch.setattr(type_gate.os, "sched_getaffinity", unavailable, raising=False)
    monkeypatch.setattr(type_gate.os, "cpu_count", lambda: cpus)
    monkeypatch.setattr(type_gate.os, "getloadavg", lambda: (0.0, 0.0, 0.0), raising=False)
    assert _measure_workers(tmp_path, monkeypatch) == expected


@pytest.mark.feature("type-gate-cpu-workers")
@pytest.mark.parametrize("load_missing", [True, False], ids=["missing-load", "failed-load"])
def test_measure_uses_one_worker_when_load_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, load_missing: bool
) -> None:
    monkeypatch.delenv("AGENTIHOOKS_TYPE_WORKERS", raising=False)
    monkeypatch.setattr(type_gate.os, "sched_getaffinity", lambda _pid: set(range(16)), raising=False)

    def unavailable() -> tuple[float, float, float]:
        raise OSError("CPU load unavailable")

    if load_missing:
        monkeypatch.delattr(type_gate.os, "getloadavg", raising=False)
    else:
        monkeypatch.setattr(type_gate.os, "getloadavg", unavailable, raising=False)
    assert _measure_workers(tmp_path, monkeypatch) == 1
