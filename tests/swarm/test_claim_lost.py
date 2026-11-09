from typing import Any

import pytest

from scripts.swarm import claim_lost

pytestmark = pytest.mark.unit

NAME = "ci@a1b2c3-0003"


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"claimed_by": NAME}, False),
        ({"claimed_by": "ci@a1b2c3-0007"}, True),
        ({"claimed_by": ""}, True),
        ({}, False),
        ({"state": "open"}, True),
        (None, False),
    ],
)
def test_a_claim_is_lost_only_when_the_ledger_stops_naming_the_worker(
    row: dict[str, Any] | None, expected: bool
) -> None:
    assert claim_lost.lost(NAME, row) is expected


def test_a_null_claimant_counts_as_none() -> None:
    assert claim_lost.lost(NAME, {"claimed_by": None}) is True
