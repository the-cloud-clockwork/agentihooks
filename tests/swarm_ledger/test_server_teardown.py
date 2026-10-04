import time

import pytest

from tests.swarm_ledger import test_bin, test_swarm_panel


@pytest.mark.parametrize("case", [test_bin.BinEndpoint, test_swarm_panel.SwarmPanel], ids=lambda c: c.__name__)
def test_the_test_server_stops_without_waiting_half_a_second(case):
    case.setUpClass()
    started = time.monotonic()
    case.tearDownClass()
    assert time.monotonic() - started < 0.25
