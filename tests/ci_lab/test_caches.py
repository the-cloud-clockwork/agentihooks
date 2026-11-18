import pytest

from scripts.ci_lab import caches


@pytest.mark.feature("lab-scheduler-metrics")
def test_a_report_counts_git_hits_and_misses_per_revision_and_uv_per_venv() -> None:
    assert caches.report([False, True, False], [True]) == {
        "git": {"hits": 2, "misses": 1},
        "uv": {"hits": 0, "misses": 1},
    }
    assert caches.report([], [False, False]) == {"git": {"hits": 0, "misses": 0}, "uv": {"hits": 2, "misses": 0}}


@pytest.mark.feature("lab-scheduler-metrics")
def test_read_takes_each_cache_a_unit_report_carries_and_nothing_from_an_older_worker() -> None:
    report = {"seconds": 1.0, "caches": caches.report([True, False], [False])}
    assert caches.read(report) == {"git": (1, 1), "uv": (1, 0)}
    assert caches.read({"caches": {"uv": {"hits": 3, "misses": 2}, "other": {"hits": 9, "misses": 9}}}) == {
        "uv": (3, 2)
    }
    assert caches.read({"seconds": 1.0}) == {}
