import pytest

from scripts import quota_freshness


@pytest.mark.parametrize(
    "observed_at,resets,now,stale",
    [
        (None, (), 1_000.0, True),
        (100.0, (), 100.0 + 900, False),
        (100.0, (), 100.0 + 901, True),
        (100.0, (None, None), 200.0, False),
        (100.0, (None, 200.0), 200.0, True),
        (100.0, (201.0,), 200.0, False),
        (100.0, (5_000.0, 150.0), 200.0, True),
        (100.0, (150.0, 5_000.0), 200.0, True),
    ],
)
def test_a_reading_is_stale_when_missing_past_the_freshness_window_or_taken_before_a_passed_reset(
    observed_at, resets, now, stale
):
    assert quota_freshness.stale(observed_at, resets, now) is stale


def test_the_age_reads_whole_seconds_since_the_reading_in_the_balance_span_format():
    assert quota_freshness.age(None, 1_000.0) == "never read"
    assert quota_freshness.age(100.9, 160.5) == "1m"
    assert quota_freshness.age(100.0, 159.0) == "0m"
    assert quota_freshness.age(100.0, 100.0 + 3_600 + 120) == "1h02m"
    assert quota_freshness.age(500.0, 100.0) == "0m"
