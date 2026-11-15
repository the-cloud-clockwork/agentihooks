import json

import pytest

from scripts.ci_lab import capacity
from scripts.ci_lab.capacity import Load
from scripts.ci_lab.delivery import CLASSES


@pytest.mark.parametrize(
    ("estimates", "free", "expected"),
    [
        ((), 0, 0.0),
        ((), 4, 0.0),
        ((120.0,), 1, 120.0),
        ((120.0, 60.0, 30.0), 3, 70.0),
        ((90.0, 90.0), 4, 45.0),
        ((10.0,), 0, None),
        ((10.0,), -2, None),
    ],
)
def test_predicted_wait_is_pending_seconds_over_free_slots(
    estimates: tuple[float, ...], free: int, expected: float | None
) -> None:
    assert capacity.predicted_wait(estimates, free) == expected


def test_free_slots_never_go_below_zero() -> None:
    assert Load({}, {}, slots_total=2, slots_busy=5).slots_free == 0
    assert Load({}, {}, slots_total=8, slots_busy=3).slots_free == 5


@pytest.mark.parametrize(
    ("total", "busy", "expected"),
    [(0, 0, 0.0), (0, 3, 0.0), (1, 1, 1.0), (8, 2, 0.25), (4, 4, 1.0), (4, 6, 1.0)],
)
def test_utilisation_is_busy_over_total(total: int, busy: int, expected: float) -> None:
    assert capacity.utilisation(Load({}, {}, slots_total=total, slots_busy=busy)) == expected


@pytest.mark.feature("lab-capacity-verdict-cache-hits")
def test_capacity_document_reports_every_class() -> None:
    load = Load(
        pending={"pull_request": (300.0, 100.0), "backfill": (600.0,)},
        running={"pull_request": 3, "queue": 1},
        slots_total=6,
        slots_busy=4,
        budget_fraction=0.4,
        shed=("backfill",),
    )
    body = capacity.document(load)
    assert body == {
        "classes": {
            "hotfix": {"pending": 0, "running": 0, "predicted_wait_s": 0.0, "verdict_cache_hit_ratio": None},
            "pull_request": {"pending": 2, "running": 3, "predicted_wait_s": 200.0, "verdict_cache_hit_ratio": None},
            "queue": {"pending": 0, "running": 1, "predicted_wait_s": 0.0, "verdict_cache_hit_ratio": None},
            "promotion": {"pending": 0, "running": 0, "predicted_wait_s": 0.0, "verdict_cache_hit_ratio": None},
            "backfill": {"pending": 1, "running": 0, "predicted_wait_s": 300.0, "verdict_cache_hit_ratio": None},
        },
        "slots": {"total": 6, "busy": 4, "free": 2},
        "utilisation": 4 / 6,
        "budget_fraction": 0.4,
        "shed": ["backfill"],
    }
    assert list(body["classes"]) == list(CLASSES)  # type: ignore[call-overload]
    json.dumps(body, allow_nan=False)


def test_a_full_pool_reports_no_wait_estimate_and_classes_in_shed_order() -> None:
    load = Load({"queue": (5.0,)}, {}, slots_total=2, slots_busy=2, shed=("queue", "backfill", "promotion"))
    body = capacity.document(load)
    assert body["shed"] == ["queue", "promotion", "backfill"]
    assert body["classes"]["queue"]["predicted_wait_s"] is None  # type: ignore[index]
    assert body["classes"]["hotfix"]["predicted_wait_s"] == 0.0  # type: ignore[index]
    json.dumps(body, allow_nan=False)


def test_load_defaults_to_a_full_budget_and_nothing_shed() -> None:
    body = capacity.document(Load({}, {}, slots_total=1, slots_busy=0))
    assert body["budget_fraction"] == 1.0
    assert body["shed"] == []


@pytest.mark.feature("lab-capacity-verdict-cache-hits")
@pytest.mark.parametrize(
    ("hits", "misses", "expected"),
    [(0, 0, None), (1, 0, 1.0), (0, 4, 0.0), (3, 1, 0.75), (1, 4, 0.2)],
)
def test_hit_ratio_is_hits_over_lookups(hits: int, misses: int, expected: float | None) -> None:
    assert capacity.hit_ratio(hits, misses) == expected


@pytest.mark.feature("lab-capacity-verdict-cache-hits")
def test_capacity_document_reports_the_verdict_cache_hit_ratio_per_class() -> None:
    load = Load(
        {},
        {},
        slots_total=2,
        slots_busy=0,
        cache_hits={"pull_request": 3, "queue": 2, "unknown": 9},
        cache_misses={"pull_request": 1, "promotion": 5, "unknown": 1},
    )
    body = capacity.document(load)
    ratios = {name: entry["verdict_cache_hit_ratio"] for name, entry in body["classes"].items()}  # type: ignore[union-attr]
    assert ratios == {"hotfix": None, "pull_request": 0.75, "queue": 1.0, "promotion": 0.0, "backfill": None}
    json.dumps(body, allow_nan=False)


@pytest.mark.feature("lab-capacity-verdict-cache-hits")
def test_load_defaults_to_no_verdict_cache_lookups() -> None:
    load = Load({}, {}, slots_total=1, slots_busy=0)
    assert (load.cache_hits, load.cache_misses) == ({}, {})
