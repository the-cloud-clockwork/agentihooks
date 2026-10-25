from unittest.mock import Mock

import pytest

from scripts.swarm import controller
from scripts.swarm.tick import ACK_LATER, _room_wait, _spawn_order
from scripts.swarm_v2.runtime.base import RuntimeRouter
from scripts.swarm_v2.runtime.routed import RoutedRuntime
from tests.hive.test_controller_launch import request_for, setup, write_hive  # noqa: F401
from tests.swarm.test_tick import FakeLedger


def ordered(world, runtime, task):
    ledger = FakeLedger([task])
    return _spawn_order(
        world.config.slug, world.config, world.store, [], ledger.rows, ledger.state(world.config.slug), runtime
    )


@pytest.mark.parametrize("refused", [False, True])
def test_ordering_and_spawn_reuse_one_placement_and_the_next_pass_refreshes(setup, refused):  # noqa: F811
    task = {"id": "task", "title": "Launch on a hive"}
    if refused:
        write_hive(setup, excluded=["engineer"])
    original = RoutedRuntime(setup.herdr, RuntimeRouter([]))
    runtime = controller.FencedRuntime(setup.store, setup.config.slug, setup.held, original, "compose")
    launch = runtime.runtime.hive_launch
    launch.opening = setup.launcher.opening
    launch.hives = Mock(wraps=launch.hives)

    queue, waits = ordered(setup, runtime, task)
    assert len(queue) == (0 if refused else 1)
    assert bool(waits) is refused
    assert launch.hives.call_count == 1
    first = launch.select(setup.config, "eng", task)
    if not refused:
        assert _room_wait(runtime, "eng", task) == ""
        request = request_for(setup)
        request.task[ACK_LATER] = True
        runtime.spawn(setup.config, "eng", request.name, request.task)
        order = launch.orders_for(request.task["id"])
        assert order.hive == first.hive
        setup.store.drop_agent(setup.config.slug, request.name)
    assert launch.hives.call_count == 1

    write_hive(setup, excluded=[] if refused else ["engineer"])
    queue, waits = ordered(setup, runtime, task)
    assert len(queue) == (1 if refused else 0)
    assert bool(waits) is not refused
    assert launch.hives.call_count == 2
