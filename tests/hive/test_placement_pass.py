from unittest.mock import Mock

import pytest

from scripts.swarm import controller
from scripts.swarm.tick import ACK_LATER, SpawnError, _room_wait, _spawn_order
from scripts.swarm_v2.runtime.base import RuntimeRouter
from scripts.swarm_v2.runtime.routed import RoutedRuntime
from tests.hive.test_controller_launch import request_for, setup, write_hive  # noqa: F401
from tests.swarm.test_tick import FakeLedger


def test_cached_placements_keep_each_task_and_lane_separate(setup):  # noqa: F811
    from dataclasses import replace

    from scripts.hive.placement import Wait

    setup.config = replace(setup.config, lanes={"ci": {"agent": "codex"}})
    original = RoutedRuntime(setup.herdr, RuntimeRouter([]))
    runtime = controller.FencedRuntime(setup.store, setup.config.slug, setup.held, original, "compose")
    launch = runtime.runtime.hive_launch
    launch.hives = Mock(wraps=launch.hives)
    one, two = {"id": "one"}, {"id": "two"}
    ordered(setup, runtime, one)
    first = launch.select(setup.config, "eng", one)
    second = launch.select(setup.config, "eng", two)
    ci = launch.select(setup.config, "ci", one)
    assert first.account.name == second.account.name == "account"
    assert isinstance(ci, Wait)
    assert launch.hives.call_count == 3
    assert launch.select(setup.config, "eng", one) is first
    assert launch.select(setup.config, "eng", two) is second
    assert launch.select(setup.config, "ci", one) is ci
    assert launch.hives.call_count == 3


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
        with pytest.raises(SpawnError, match="hive launch ordered"):
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


def test_spawn_pass_ends_the_placement_cache_for_later_launches(setup):  # noqa: F811
    from scripts.swarm.tick import _placement_pass, _spawn

    task = {"id": "task", "title": "Launch on a hive"}
    write_hive(setup, excluded=["engineer"])
    original = RoutedRuntime(setup.herdr, RuntimeRouter([]))
    runtime = controller.FencedRuntime(setup.store, setup.config.slug, setup.held, original, "compose")
    launch = runtime.runtime.hive_launch
    launch.hives = Mock(wraps=launch.hives)
    ledger = FakeLedger([task])
    slug = setup.config.slug

    with _placement_pass(runtime):
        _spawn(slug, setup.config, setup.store, ledger, runtime, ledger.rows, ledger.state(slug), 0)
        assert launch.placement_cache
    assert launch.hives.call_count == 1
    assert launch.placement_cache is None

    write_hive(setup, excluded=[])
    assert launch.select(setup.config, "eng", task).hive
    assert launch.hives.call_count == 2
