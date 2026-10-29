import pytest

from scripts.swarm import tick


@pytest.mark.parametrize(
    ("taken", "expected"),
    [
        (set(), "eng-1@rig"),
        ({"eng-1@rig"}, "eng-2@rig"),
        ({"eng-1@rig", "eng-3@rig", "ci-1@rig"}, "eng-2@rig"),
    ],
)
def test_free_seat_selects_the_first_unoccupied_generated_label(taken, expected):
    assert tick._free_seat("rig", "eng", taken, "") == expected


def test_free_seat_keeps_an_available_preferred_address():
    assert tick._free_seat("rig", "eng", {"eng-1@rig"}, "eng-8@rig") == "eng-8@rig"


def test_free_seat_replaces_an_occupied_preferred_address():
    assert tick._free_seat("rig", "eng", {"eng-1@rig"}, "eng-1@rig") == "eng-2@rig"
