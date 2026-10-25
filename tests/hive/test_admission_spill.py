import json
from dataclasses import replace
from unittest.mock import Mock

import pytest

from scripts.hive import admission, headroom
from scripts.swarm_v2.runtime.base import Status
from tests.hive.test_controller_launch import request_for, setup, write_hive  # noqa: F401


@pytest.mark.parametrize("harness", ["claude", "codex"])
@pytest.mark.parametrize("in_pass", [False, True])
@pytest.mark.parametrize(
    "reading",
    [
        {"cpus": 1, "load5": 2.0},
        {"available_mb": 0},
        {"disk_free_gb": 1, "disk_floor_gb": 50},
    ],
    ids=["cpu", "memory", "disk"],
)
def test_resource_refusal_spills_to_the_next_ranked_hive(setup, monkeypatch, in_pass, reading, harness, capsys):  # noqa: F811
    setup.config.lanes["eng"] = {"agent": harness}
    setup.herdr._quota_accounts = [replace(row, harness=harness) for row in setup.herdr._quota_accounts]
    slots = [{"harness": harness, "account": "account", "kind": "subscription"}]
    write_hive(setup, prefer={"engineer": 0}, slots=slots)
    write_hive(setup, id="spare", prefer={"engineer": 1}, slots=slots)
    launcher = setup.launcher
    launcher.placement_cache = {} if in_pass else None
    request = request_for(setup)
    monkeypatch.setattr(launcher, "wait", launcher.poll)
    assert launcher.select(setup.config, "eng", request.task).hive == "remote"
    reserve = launcher.reserve_room
    attempted = []

    def refuse_first(request, selected):
        attempted.append(selected.hive)
        if selected.hive == "remote":
            key = headroom.key("remote")
            measured = json.loads(setup.store.redis.get(key))
            setup.store.redis.set(key, json.dumps({**measured, **reading}))
        return reserve(request, selected)

    monkeypatch.setattr(launcher, "reserve_room", refuse_first)
    result = launcher.spawn(request)
    assert result.status is Status.PENDING
    assert attempted == ["remote", "spare"]
    order = launcher.orders_for(request.task["id"])
    assert order.hive == "spare"
    assert setup.store.redis.xlen(launcher.orders.stream("remote")) == 0
    assert admission.pending(setup.store.redis, "remote") == 0
    assert admission.pending(setup.store.redis, "spare") == 1
    selected = launcher.select(setup.config, "eng", request.task)
    assert selected.hive == "spare"
    assert selected.account.harness == harness
    captured = capsys.readouterr()
    assert captured.out == ""
    logged = json.loads(captured.err)
    reason = logged.pop("reason")
    assert logged == {
        "event": "hive_admission_refused",
        "slug": "fixture",
        "task": "task",
        "name": request.name,
        "hive": "remote",
    }
    assert reason.startswith("no hive for eng has room: remote ")
    assert reason.split(": ", 2)[2]


def test_spill_does_not_bypass_hive_role_constraints(setup, monkeypatch):  # noqa: F811
    write_hive(setup, id="spare", excluded=["engineer"])
    launcher = setup.launcher
    refused = "no hive for eng has room: remote cpu: no CPU available"
    reserve = Mock(return_value=refused)
    monkeypatch.setattr(launcher, "reserve_room", reserve)
    request = request_for(setup)
    result = launcher.spawn(request)
    assert result.status is Status.UNAVAILABLE
    assert result.detail == refused
    assert reserve.call_count == 1
    assert launcher.orders_for(request.task["id"]) is None


@pytest.mark.parametrize("in_pass", [False, True])
def test_a_full_launch_queue_spills_without_reserving_the_refused_hive(setup, monkeypatch, in_pass):  # noqa: F811
    write_hive(setup, prefer={"engineer": 0})
    write_hive(setup, id="spare", prefer={"engineer": 1})
    launcher = setup.launcher
    launcher.placement_cache = {} if in_pass else None
    stream = launcher.orders.stream("remote")
    for _ in range(4):
        setup.store.redis.xadd(stream, {"order": "{}"})
    request = request_for(setup)
    monkeypatch.setattr(launcher, "wait", launcher.poll)
    result = launcher.spawn(request)
    assert result.status is Status.PENDING
    assert launcher.orders_for(request.task["id"]).hive == "spare"
    assert setup.store.redis.xlen(stream) == 4
    assert admission.pending(setup.store.redis, "remote") == 0


@pytest.mark.parametrize("in_pass", [False, True])
def test_all_refused_hives_are_tried_once_and_keep_the_last_refusal(setup, monkeypatch, in_pass):  # noqa: F811
    write_hive(setup, prefer={"engineer": 0})
    write_hive(setup, id="spare", prefer={"engineer": 1})
    launcher = setup.launcher
    launcher.placement_cache = {} if in_pass else None
    reserve = Mock(side_effect=lambda request, selected: f"no hive for eng has room: {selected.hive} cpu: full")
    monkeypatch.setattr(launcher, "reserve_room", reserve)
    request = request_for(setup)
    result = launcher.spawn(request)
    assert result.status is Status.UNAVAILABLE
    assert result.detail == "no hive for eng has room: spare cpu: full"
    assert [call.args[1].hive for call in reserve.call_args_list] == ["remote", "spare"]
    assert launcher.orders_for(request.task["id"]) is None


def test_prompt_failure_releases_the_admission_selected_after_spill(setup, monkeypatch):  # noqa: F811
    write_hive(setup, prefer={"engineer": 0})
    write_hive(setup, id="spare", prefer={"engineer": 1})
    launcher = setup.launcher
    reserve = launcher.reserve_room
    monkeypatch.setattr(
        launcher,
        "reserve_room",
        lambda request, selected: "remote cpu: full" if selected.hive == "remote" else reserve(request, selected),
    )
    launcher.opening = Mock(side_effect=RuntimeError("prompt failed"))
    with pytest.raises(RuntimeError, match="prompt failed"):
        launcher.spawn(request_for(setup))
    assert admission.pending(setup.store.redis, "spare") == 0
