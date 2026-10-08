import pytest

from scripts import session_bands
from scripts.session_bands import Seat

NOW = 1_800_000_000


@pytest.mark.parametrize(
    ("five_left", "seats"),
    [(100, 6), (60, 6), (59.9, 4), (40, 4), (39.9, 3), (10, 3), (9.9, 2), (5, 2), (4.9, 0), (0, 0)],
)
def test_the_five_hour_window_alone_sets_the_cap(five_left, seats):
    assert session_bands.cap(five_left, 50) == seats


@pytest.mark.parametrize(("week_left", "seats"), [(5, 6), (4.9, 0), (0, 0)])
def test_a_week_under_five_percent_takes_no_new_session(week_left, seats):
    assert session_bands.cap(100, week_left) == seats
    assert session_bands.week_cap(week_left) == seats


def test_a_missing_reading_has_no_cap():
    assert session_bands.cap(None, 50) is None
    assert session_bands.cap(50, None) is None
    assert session_bands.week_cap(None) is None


def test_todays_claude_accounts_and_codex():
    today = {
        "ncgma": (75, 93),
        "nchotma": (95, 8),
        "ncsmgma": (100, 4),
        "nctcc": (100, 12),
        "tccgma": (80, 19),
    }
    assert {name: session_bands.cap(*left) for name, left in today.items()} == {
        "ncgma": 6,
        "nchotma": 6,
        "ncsmgma": 0,
        "nctcc": 6,
        "tccgma": 6,
    }
    assert session_bands.week_cap(26) == 6


def test_a_passed_reset_reads_as_a_full_window():
    assert session_bands.left(96, NOW, NOW) == 100.0
    assert session_bands.left(96, NOW + 1, NOW) == 4.0
    assert session_bands.left(96, None, NOW) == 4.0
    assert session_bands.left(None, None, NOW) is None
    assert session_bands.left(None, NOW - 1, NOW) == 100.0
    assert session_bands.left(120, None, NOW) == 0.0


def test_a_reading_older_than_fifteen_minutes_is_stale():
    assert session_bands.fresh(NOW - 900, NOW)
    assert not session_bands.fresh(NOW - 901, NOW)
    assert not session_bands.fresh(None, NOW)


def test_the_next_session_goes_to_the_eligible_account_with_the_fewest_sessions():
    seats = [
        Seat("claude", "ncgma", 6, 4),
        Seat("claude", "nchotma", 6, 2),
        Seat("claude", "ncsmgma", 0, 0),
        Seat("claude", "tccgma", 6, 3),
        Seat("codex", "default", 6, 2),
    ]
    assert session_bands.pick(seats) == seats[1]
    seats[1] = Seat("claude", "nchotma", 2, 2)
    assert session_bands.pick(seats) == seats[4]
    assert session_bands.pick([Seat("claude", "a", 2, 2), Seat("claude", "b", 0, 0)]) is None


def test_ties_rotate_in_a_fixed_order_as_sessions_land():
    seats = {name: Seat("claude", name, 3, 0) for name in ("b", "a")}
    order = []
    for _ in range(4):
        chosen = session_bands.pick(seats.values())
        order.append(chosen.account)
        seats[chosen.account] = Seat("claude", chosen.account, 3, chosen.sessions + 1)
    assert order == ["a", "b", "a", "b"]


def test_a_seat_counts_its_free_places():
    assert Seat("claude", "a", 6, 4).free == 2
    assert Seat("claude", "a", 2, 3).free == 0


def test_among_equal_sessions_the_soonest_week_reset_wins():
    seats = [
        Seat("claude", "a", 6, 1, NOW + 5 * 86400),
        Seat("claude", "b", 6, 1, NOW + 3600),
        Seat("codex", "c", 6, 1),
        Seat("claude", "d", 6, 0, NOW + 6 * 86400),
    ]
    assert session_bands.pick(seats) == seats[3]
    seats[3] = Seat("claude", "d", 6, 1, NOW + 6 * 86400)
    assert session_bands.pick(seats) == seats[1]
    seats[1] = Seat("claude", "b", 1, 1, NOW + 3600)
    assert session_bands.pick(seats) == seats[0]
    seats[0] = Seat("claude", "a", 1, 1, NOW)
    assert session_bands.pick(seats) == seats[3]
    assert session_bands.pick([Seat("claude", "z", 6, 1), Seat("codex", "y", 6, 1, NOW)]).account == "y"
    assert session_bands.pick([Seat("codex", "c", 6, 1), Seat("claude", "z", 6, 1)]).account == "z"


@pytest.mark.parametrize(("five_left", "spend_by"), [(5.1, NOW), (None, NOW), (5, None), (0, None)])
def test_only_an_account_above_the_five_hour_handoff_margin_spends_its_week_first(five_left, spend_by):
    assert session_bands.spend_by(five_left, NOW) == spend_by


def test_only_a_reset_still_ahead_counts_as_upcoming():
    assert session_bands.upcoming(NOW + 1, NOW) == NOW + 1
    assert session_bands.upcoming(NOW, NOW) is None
    assert session_bands.upcoming(None, NOW) is None
