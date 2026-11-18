import pytest

from scripts.ci_lab import outcomes


@pytest.mark.feature("lab-unit-outcomes")
def test_only_the_queue_and_the_scheduler_set_lost_and_dropped() -> None:
    assert outcomes.LOST not in outcomes.COMPLETED
    assert outcomes.DROPPED not in outcomes.COMPLETED
    assert outcomes.LOSSES == {outcomes.LOST, outcomes.CRASH}
    assert outcomes.LOSSES - outcomes.COMPLETED == {outcomes.LOST}
