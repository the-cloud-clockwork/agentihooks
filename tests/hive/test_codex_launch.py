import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.hive.test_controller_launch import request_for, write_hive
from tests.hive.test_controller_launch import setup as setup


@pytest.mark.parametrize("lane", ["eng", "ci"])
def test_quota_allocated_codex_launch_keeps_its_harness_through_receipt(setup, monkeypatch, lane):
    from scripts.hive import launch
    from scripts.hive.orders import Consumer
    from scripts.swarm.capacity import Account
    from scripts.swarm.tick import Placed
    from scripts.swarm_v2.runtime.base import Outcome, Status

    setup.herdr._quota_tasks = {"task": "codex"}
    setup.herdr._quota_accounts = [
        Account("claude", "account", "room", 6, 80, 80, 6),
        Account("codex", "codex-account", "room", 0, None, 80, 6),
    ]
    setup.herdr.planned_account.side_effect = lambda task, harness: "codex-account" if harness == "codex" else None
    write_hive(setup, slots=[{"harness": "codex", "account": "codex-account", "kind": "subscription"}])
    request = request_for(setup, lane=lane)
    request = replace(request, config=replace(request.config, lanes={lane: {"agent": "auto"}}))
    runtime = Mock()
    runtime.spawn.return_value = Outcome(
        "spawn",
        Status.OK,
        "local",
        Placed("pane", "codex", "codex-account", profile="engineer", profile_decision={"validation": {"pid": 12}}),
    )
    runtime.namespace.return_value = "namespace"
    runtime.table.return_value = {12: SimpleNamespace(start_time=34)}
    consumer = Consumer(setup.store.redis, "remote", runtime)
    setup.launcher.sleep = lambda _: consumer.poll()

    outcome = setup.launcher.spawn(request)

    assert outcome.ok, outcome.detail
    order = setup.launcher.orders_for("task")
    assert (order.harness, order.slot, order.hive) == ("codex", "codex-account", "remote")
    agent = outcome.value.execution
    assert (agent.harness, agent.account, agent.profile, agent.state) == (
        "codex",
        "codex-account",
        "engineer",
        "working",
    )
    assert outcome.value.harness == "codex"
    launched = runtime.spawn.call_args.args[0]
    assert launched.task["launch_assignment"]["harness"] == "codex"
    assert launched.task["launch_assignment"]["account"] == "codex-account"
    assert launched.config.lanes[lane]["agent"] == "codex"
    assert json.loads(setup.store.redis.get(order.prompt_ref))["hive_opening"] == f"opening for {request.name}"
    assert setup.launcher.slots.counts() == {"codex:codex-account": 1}
    launch.release(setup.store, "fixture", agent)
    assert setup.launcher.slots.counts() == {}
