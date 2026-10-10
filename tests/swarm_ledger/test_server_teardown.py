import time

import pytest

from tests.swarm_ledger import test_bin, test_swarm_panel

CASES = [test_bin.BinEndpoint, test_swarm_panel.SwarmPanel]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.__name__)
def test_the_test_server_starts_and_stops(case):
    case.setUpClass()
    case.tearDownClass()


@pytest.mark.wall_clock
@pytest.mark.parametrize("case", CASES, ids=lambda c: c.__name__)
def test_the_test_server_stops_without_waiting_half_a_second(case):
    case.setUpClass()
    started = time.monotonic()
    case.tearDownClass()
    assert time.monotonic() - started < 0.25
