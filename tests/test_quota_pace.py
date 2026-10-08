import pytest

from scripts import quota_pace
from scripts.claude_quota_balancer import QuotaWindow

NOW = 1_800_000_000
HOUR = 3600


def window(left, hours):
    return QuotaWindow(used=100 - left, resets_at=NOW + int(hours * HOUR))


FIVE = window(100, 2)
RESETTING = window(4, 5)
SPENT_LONG = window(19, 96)
FRESH = window(100, 168)


def test_a_week_about_to_reset_outranks_a_fresh_week_and_a_long_spent_week_ranks_below_it():
    rates = {
        name: quota_pace.rate(FIVE, [week], NOW)
        for name, week in [("resetting", RESETTING), ("spent", SPENT_LONG), ("fresh", FRESH)]
    }
    assert rates["resetting"] == pytest.approx(0.8)
    assert rates["fresh"] == pytest.approx(100 / 168)
    assert rates["spent"] == pytest.approx(19 / 96)
    assert sorted(rates, key=rates.get, reverse=True) == ["resetting", "fresh", "spent"]


def test_states_follow_the_pace_of_each_week():
    assert quota_pace.state(FIVE, [RESETTING], NOW) == "NORMAL"
    assert quota_pace.state(FIVE, [FRESH], NOW) == "NORMAL"
    assert quota_pace.state(FIVE, [window(40, 72)], NOW) == "NORMAL"
    assert quota_pace.state(FIVE, [window(19, 72)], NOW) == "REDUCE"
    assert quota_pace.state(FIVE, [SPENT_LONG], NOW) == "REDUCE"
    assert quota_pace.state(FIVE, [window(10, 96)], NOW) == "DRAIN"
    assert quota_pace.state(FIVE, [window(0, 1)], NOW) == "DRAIN"


@pytest.mark.parametrize(("used", "expected"), [(64, "NORMAL"), (65, "REDUCE"), (80, "DRAIN_SOON"), (90, "DRAIN")])
def test_the_five_hour_window_guards_a_healthy_week(used, expected):
    assert quota_pace.state(QuotaWindow(used=used, resets_at=NOW + HOUR), [FRESH], NOW) == expected


def test_the_worse_of_the_two_windows_wins():
    assert quota_pace.state(QuotaWindow(used=70, resets_at=NOW), [window(10, 96)], NOW) == "DRAIN"
    assert quota_pace.state(QuotaWindow(used=85, resets_at=NOW), [SPENT_LONG], NOW) == "DRAIN_SOON"


def test_an_exhausted_five_hour_window_spends_nothing():
    assert quota_pace.rate(QuotaWindow(used=96, resets_at=NOW + HOUR), [FRESH], NOW) == 0.0
    assert not quota_pace.routable(QuotaWindow(used=96, resets_at=NOW + HOUR), [FRESH], NOW)


def test_unknown_windows_have_no_rate():
    assert quota_pace.rate(QuotaWindow(), [FRESH], NOW) is None
    assert quota_pace.rate(FIVE, [QuotaWindow()], NOW) is None
    assert quota_pace.state(FIVE, [QuotaWindow()], NOW) == "UNKNOWN"
    assert not quota_pace.routable(FIVE, [QuotaWindow()], NOW)


def test_a_week_without_a_reset_time_is_read_as_a_full_week():
    assert quota_pace.rate(FIVE, [QuotaWindow(used=16)], NOW) == pytest.approx(84 / 168)


def test_a_passed_or_imminent_reset_counts_as_one_hour():
    assert quota_pace.rate(FIVE, [window(3, -2)], NOW) == pytest.approx(3.0)
    assert quota_pace.rate(FIVE, [window(3, 0.1)], NOW) == pytest.approx(3.0)


def test_the_slowest_weekly_window_sets_the_rate():
    assert quota_pace.rate(FIVE, [FRESH, RESETTING], NOW) == pytest.approx(100 / 168)


def test_a_nearly_empty_week_is_routable_only_while_it_can_be_spent_at_full_pace():
    assert quota_pace.routable(FIVE, [RESETTING], NOW)
    assert not quota_pace.routable(FIVE, [window(4, 10)], NOW)
    assert quota_pace.routable(FIVE, [window(5, 160)], NOW)
    assert not quota_pace.routable(FIVE, [window(0, 1)], NOW)


def test_the_five_hour_routing_minimum_is_inclusive():
    assert quota_pace.routable(QuotaWindow(used=95), [FRESH], NOW)
    assert not quota_pace.routable(QuotaWindow(used=95.1), [FRESH], NOW)
