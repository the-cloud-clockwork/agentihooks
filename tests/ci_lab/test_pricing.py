import json
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.ci_lab import pricing, units
from scripts.ci_mutation.plan import MUTANT_SECONDS
from tests.queue_shards import SLOWDOWN


class History:
    def __init__(self, kind: str, key: str, seconds: tuple[float, ...]) -> None:
        self.kind = kind
        self.key = key
        self.seconds = seconds

    def durations(self, kind: str, key: str) -> tuple[float, ...]:
        assert (kind, key) == (self.kind, self.key)
        return self.seconds


def test_test_estimate_uses_snapshot_and_slowdown(tmp_path: Path) -> None:
    snapshot = tmp_path / ".test_durations"
    snapshot.write_text(json.dumps({"tests/a.py::test_a": 2.0, "tests/a.py::test_b": 7.0}))
    unit = units.Unit(kind="test", key="batch", members=("tests/a.py::test_a", "tests/a.py::test_b"))
    assert pricing.estimate(unit, snapshot, History("test", "batch", ())) == 9.0 * SLOWDOWN
    snapshot.write_text(json.dumps({"tests/a.py::test_a": 3.0, "tests/a.py::test_b": 8.0}))
    assert pricing.estimate(unit, snapshot, History("test", "batch", ())) == 11.0 * SLOWDOWN


def test_mutant_estimate_uses_constant_until_history_exists(tmp_path: Path) -> None:
    unit = units.Unit(kind="mutant", key="function", members=("mutant_1", "mutant_2", "mutant_3"))
    assert pricing.estimate(unit, tmp_path / "absent", History("mutant", "function", ())) == 3 * MUTANT_SECONDS


@pytest.mark.parametrize("kind", ["test", "mutant", "static", "stats"])
def test_measured_history_overrides_bootstrap(kind: str, tmp_path: Path) -> None:
    unit = units.Unit(kind=kind, key="measured", members=("item",))
    assert pricing.estimate(unit, tmp_path / "absent", History(kind, "measured", (2.0, 4.0, 12.0))) == 6.0
    assert pricing.estimate(unit, tmp_path / "absent", History(kind, "measured", (0.0,))) == 0.0


def test_unknown_test_uses_queue_snapshot_fallback(tmp_path: Path) -> None:
    snapshot = tmp_path / ".test_durations"
    snapshot.write_text(json.dumps({"tests/a.py::test_a": 2.0, "tests/a.py::test_b": 7.0, "tests/b.py::test_c": 11.0}))
    unit = units.Unit(kind="test", key="new", members=("tests/a.py::test_new", "tests/new.py::test_new"))
    assert pricing.estimate(unit, snapshot, History("test", "new", ())) == 18.0 * SLOWDOWN


def test_unmeasured_other_kind_has_no_typed_estimate(tmp_path: Path) -> None:
    unit = units.Unit(kind="static", key="lint", members=())
    with pytest.raises(ValueError) as error:
        pricing.estimate(unit, tmp_path / "absent", History("static", "lint", ()))
    assert str(error.value) == "No measured duration for static:lint"


@pytest.mark.parametrize(("cpus", "quota", "expected"), [(8, 2.9, 2), (2, 8.0, 2), (4, None, 4), (8, 0.5, 1)])
def test_slot_weight_uses_cpu_quota(cpus: int, quota: float | None, expected: int) -> None:
    with (
        patch.object(pricing.os, "cpu_count", return_value=cpus),
        patch.object(pricing.ci_cpu_quota, "quota_cpus", return_value=quota) as read_quota,
    ):
        assert pricing.weight() == expected
        read_quota.assert_called_once_with()


def test_slot_weight_handles_unknown_cpu_count() -> None:
    with (
        patch.object(pricing.os, "cpu_count", return_value=None),
        patch.object(pricing.ci_cpu_quota, "quota_cpus", return_value=None),
    ):
        assert pricing.weight() == 1
