import pytest

from scripts.swarm import claim_lost

pytestmark = pytest.mark.unit

NAME = "ci@a1b2c3-0003"


@pytest.mark.parametrize(
    ("row", "hive", "expected"),
    [
        ({"claimed_by": NAME}, True, False),
        ({"claimed_by": NAME}, False, False),
        ({"claimed_by": "ci@a1b2c3-0007"}, False, True),
        ({"claimed_by": "ci@a1b2c3-0007"}, True, True),
        ({"claimed_by": ""}, True, True),
        ({"claimed_by": ""}, False, False),
        ({}, True, False),
        ({"state": "open"}, True, True),
        ({"state": "open"}, False, False),
        (None, True, False),
    ],
)
def test_a_claim_is_lost_only_when_the_ledger_stops_naming_the_worker(row, hive, expected):
    assert claim_lost.lost(NAME, row, hive) is expected


def test_a_null_claimant_counts_as_none():
    assert claim_lost.lost(NAME, {"claimed_by": None}, True) is True
    assert claim_lost.lost(NAME, {"claimed_by": None}, False) is False
