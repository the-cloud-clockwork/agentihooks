import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.hive.test_controller_launch import request_for, write_hive
from tests.hive.test_controller_launch import setup as setup


@pytest.mark.parametrize("lane", ["eng", "ci"])
def test_quota_allocated_codex_launch_keeps_its_harness_through_receipt(setup, monkeypatch, lane):
    from scripts.hive import launch, orders
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
    monkeypatch.setattr(orders.prompt, "build", lambda *args, **kwargs: args[4]["harness"])
    setup.launcher.opening = orders.build_opening
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
    assert json.loads(setup.store.redis.get(order.prompt_ref))["hive_opening"] == "codex"
    assert setup.launcher.slots.counts() == {"codex:codex-account": 1}
    launch.release(setup.store, "fixture", agent)
    assert setup.launcher.slots.counts() == {}


@pytest.mark.parametrize("pin", ["lane", "saved", "planned"])
def test_codex_selection_keeps_harness_and_host_pins(setup, pin):
    from scripts.hive.placement import Wait
    from scripts.swarm.capacity import Account

    setup.herdr._quota_accounts.append(Account("codex", "codex-account", "room", 0, None, 80, 6))
    write_hive(
        setup, slots=[*setup.hive["slots"], {"harness": "codex", "account": "codex-account", "kind": "subscription"}]
    )
    task = {"id": "task", "hive": "remote"}
    config = setup.config
    if pin == "lane":
        config = replace(config, lanes={"eng": {"agent": "codex"}})
    elif pin == "saved":
        task["launch_assignment"] = {"harness": "codex", "account": "codex-account"}
    else:
        setup.herdr._quota_tasks = {"task": "codex"}
    selected = setup.launcher.select(config, "eng", task)
    assert (selected.account.harness, selected.account.name, selected.hive) == ("codex", "codex-account", "remote")
    write_hive(setup, max_agents=0)
    unavailable = setup.launcher.select(config, "eng", task)
    assert isinstance(unavailable, Wait)
    assert "room" in unavailable.reason
    assert setup.store.execution_registry.records("fixture") == []


def _unadvertised_codex(setup):
    from scripts.swarm.capacity import Account

    setup.herdr._quota_tasks = {"task": "codex"}
    setup.herdr._quota_accounts.append(Account("codex", "codex-account", "room", 0, None, 80, 6))
    setup.herdr.planned_account.side_effect = lambda task, harness: "codex-account" if harness == "codex" else None


@pytest.mark.parametrize("agent", [None, "auto"])
def test_auto_lane_falls_back_to_claude_when_no_hive_advertises_the_codex_slot(setup, agent):
    _unadvertised_codex(setup)
    lanes = {"eng": {"agent": agent}} if agent else {}
    config = replace(setup.config, lanes=lanes)

    selected = setup.launcher.select(config, "eng", {"id": "task"})

    assert (selected.account.harness, selected.account.name, selected.hive) == ("claude", "account", "remote")


@pytest.mark.parametrize("pin", ["lane", "saved"])
def test_codex_pinned_task_keeps_waiting_for_an_advertised_codex_slot(setup, pin):
    from scripts.hive.placement import Wait

    _unadvertised_codex(setup)
    task, config = {"id": "task"}, setup.config
    if pin == "lane":
        config = replace(config, lanes={"eng": {"agent": "codex"}})
    else:
        task["launch_assignment"] = {"harness": "codex", "account": "codex-account"}

    selected = setup.launcher.select(config, "eng", task)

    assert isinstance(selected, Wait)
    assert selected.reason == "no hive for eng advertises the codex slot codex-account"


def test_auto_lane_keeps_the_codex_wait_when_claude_has_no_room_either(setup):
    from scripts.hive.placement import Wait

    _unadvertised_codex(setup)
    setup.herdr._quota_accounts[0] = replace(setup.herdr._quota_accounts[0], sessions=6)

    selected = setup.launcher.select(setup.config, "eng", {"id": "task"})

    assert isinstance(selected, Wait)
    assert selected.reason == "no hive for eng advertises the codex slot codex-account"
