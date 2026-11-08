from types import SimpleNamespace

import pytest

from scripts.hive import admission, registry
from scripts.hive.launch import Selection
from scripts.swarm.tick import ACK_LATER
from scripts.swarm_v2.runtime.base import Status
from tests.hive.test_controller_launch import request_for, setup, write_hive  # noqa: F401


@pytest.mark.feature("hive-drain-launch-placement")
@pytest.mark.parametrize("removed", [False, True])
@pytest.mark.parametrize("spare", [False, True])
def test_a_cached_launch_skips_a_hive_that_started_draining_or_was_removed(
    setup: SimpleNamespace,  # noqa: F811
    removed: bool,
    spare: bool,
) -> None:
    launcher = setup.launcher
    launcher.placement_cache = {}
    if spare:
        write_hive(setup, id="spare")
    request = request_for(setup)
    request.task[ACK_LATER] = True
    selected = launcher.select(setup.config, request.lane, request.task)
    assert isinstance(selected, Selection)
    assert selected.hive == "remote"
    if removed:
        registry.forget(setup.store.redis, "remote")
    else:
        registry.update(setup.store.redis, "remote", ["draining=yes"])

    outcome = launcher.spawn(request)

    assert setup.store.redis.xlen(launcher.orders.stream("remote")) == 0
    assert admission.pending(setup.store.redis, "remote") == 0
    order = launcher.orders_for(request.task["id"])
    if spare:
        assert order.hive == "spare"
        assert setup.store.redis.xlen(launcher.orders.stream("spare")) == 1
    else:
        assert outcome.status is Status.UNAVAILABLE
        assert order is None
