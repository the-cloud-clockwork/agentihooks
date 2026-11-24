"""The one second bound of the cold load, run alone through the Proofs tests proof, because a coverage traced unit
shard sharing its runner with other workers cannot hold a one second browser bound."""

import pytest

from tests.swarm_ledger.test_cold_load import authority_live, cold_reading, live

__all__ = ["authority_live", "live"]


@pytest.mark.wall_clock
def test_a_cold_browser_shows_the_first_task_rows_within_a_second_and_a_megabyte(
    live: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_row, transferred, report = cold_reading(live, monkeypatch)
    assert first_row < 1000, report
    assert transferred < 1_000_000, report
