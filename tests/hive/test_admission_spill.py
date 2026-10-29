import json
from dataclasses import replace
from unittest.mock import Mock

import pytest

from scripts.hive import admission, headroom
from scripts.swarm.tick import ACK_LATER
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
    request.task[ACK_LATER] = True
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
def test_a_full_launch_queue_spills_without_reserving_the_refused_hive(setup, in_pass):  # noqa: F811
    write_hive(setup, prefer={"engineer": 0})
    write_hive(setup, id="spare", prefer={"engineer": 1})
    launcher = setup.launcher
    launcher.placement_cache = {} if in_pass else None
    stream = launcher.orders.stream("remote")
    for _ in range(4):
        setup.store.redis.xadd(stream, {"order": "{}"})
    request = request_for(setup)
    request.task[ACK_LATER] = True
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


def _load(launched, hive_id, **fields):
    key = headroom.key(hive_id)
    store = launched.store
    store.redis.set(key, json.dumps({**json.loads(store.redis.get(key)), **fields}))


def test_the_first_ranked_hive_takes_a_launch_under_cpu_load_below_the_memory_line(setup, capsys):  # noqa: F811
    write_hive(setup, prefer={"eng": 1})
    write_hive(setup, id="spare")
    _load(setup, "remote", cpus=1, load5=2.0)
    request = request_for(setup)
    request.task[ACK_LATER] = True
    assert setup.launcher.spawn(request).status is Status.PENDING
    order = setup.launcher.orders_for(request.task["id"])
    assert order.hive == "remote"
    assert "placement_spill" not in setup.store.execution_registry.get("fixture", order.execution_id).profile_decision
    assert "hive_placement_spill" not in capsys.readouterr().err


@pytest.mark.parametrize("in_pass", [False, True])
def test_a_spill_past_the_first_ranked_hive_records_its_memory_refusal(setup, capsys, in_pass):  # noqa: F811
    write_hive(setup, prefer={"eng": 1})
    write_hive(setup, id="spare")
    _load(setup, "remote", available_mb=20_000)
    launcher = setup.launcher
    launcher.placement_cache = {} if in_pass else None
    request = request_for(setup)
    request.task[ACK_LATER] = True
    assert launcher.spawn(request).status is Status.PENDING
    order = launcher.orders_for(request.task["id"])
    assert order.hive == "spare"
    refused = "ram: 20000 of 100000 MB available, 80 percent used, at or above the 80 percent line"
    spill = {"preferred": "remote", "refusal": refused}
    assert (
        setup.store.execution_registry.get("fixture", order.execution_id).profile_decision["placement_spill"] == spill
    )
    logged = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert logged == [
        {
            "event": "hive_placement_spill",
            "slug": "fixture",
            "task": "task",
            "name": request.name,
            "hive": "spare",
            **spill,
        }
    ]


def test_a_reservation_refused_on_the_first_ranked_hive_records_it_on_the_spill(setup, monkeypatch, capsys):  # noqa: F811
    write_hive(setup, prefer={"eng": 1})
    write_hive(setup, id="spare")
    launcher = setup.launcher
    reserve = launcher.reserve_room
    refused = "no hive for eng has room: remote ram: full"
    monkeypatch.setattr(
        launcher,
        "reserve_room",
        lambda request, selected: refused if selected.hive == "remote" else reserve(request, selected),
    )
    request = request_for(setup)
    request.task[ACK_LATER] = True
    assert launcher.spawn(request).status is Status.PENDING
    order = launcher.orders_for(request.task["id"])
    assert order.hive == "spare"
    spill = {"preferred": "remote", "refusal": refused}
    assert (
        setup.store.execution_registry.get("fixture", order.execution_id).profile_decision["placement_spill"] == spill
    )
    events = [json.loads(line)["event"] for line in capsys.readouterr().err.splitlines()]
    assert events == ["hive_admission_refused", "hive_placement_spill"]


def test_a_second_launch_in_one_pass_spills_with_the_placed_count(setup):  # noqa: F811
    write_hive(setup, prefer={"eng": 1})
    write_hive(setup, id="spare")
    _load(setup, "remote", available_mb=20_000 + admission.AGENT_MB)
    launcher = setup.launcher
    launcher.placement_cache = {}
    assert launcher.select(setup.config, "eng", {"id": "first"}).hive == "remote"
    second = launcher.select(setup.config, "eng", {"id": "second"})
    assert second.hive == "spare"
    assert second.spill.record() == {
        "preferred": "remote",
        "refusal": "placement: 1 launches placed on it this pass took its room",
    }


@pytest.mark.parametrize(
    ("fields", "refusal"),
    [
        ({"draining": "yes"}, "it is draining"),
        ({"excluded": ["master", "engineer"]}, "it excludes eng"),
        ({"excluded": ["master", "eng"]}, "it excludes eng"),
    ],
    ids=["draining", "excluding profile", "excluding lane"],
)
def test_a_launch_past_an_unhosted_first_ranked_hive_records_why(setup, fields, refusal):  # noqa: F811
    write_hive(setup, prefer={"eng": 1}, **fields)
    write_hive(setup, id="spare")
    selected = setup.launcher.select(setup.config, "eng", {"id": "task"})
    assert selected.hive == "spare"
    assert selected.spill.record() == {"preferred": "remote", "refusal": refusal}


def test_a_reservation_refused_on_the_first_ranked_hive_names_its_memory_line(setup):  # noqa: F811
    write_hive(setup, prefer={"eng": 1})
    write_hive(setup, id="spare")
    _load(setup, "remote", cpus=1, load5=2.0, available_mb=20_000 + admission.AGENT_MB)
    request = request_for(setup)
    selected = setup.launcher.select(setup.config, "eng", request.task)
    assert (selected.hive, selected.first) == ("remote", True)
    assert admission.reserve(setup.store.redis, "remote", "fixture/other", 1) == (True, 0)
    assert setup.launcher.reserve_room(request, selected) == (
        "no hive for eng has room: remote ram: 20700 of 100000 MB available, 80 percent used, "
        "700 MB per agent with 1 pending launches, fits 0 more under the 80 percent line"
    )


def test_an_acknowledged_execution_keeps_its_placement_spill(setup):  # noqa: F811
    from dataclasses import replace

    from scripts.hive import orders, placement
    from scripts.swarm import lease
    from scripts.swarm_v2.runtime.base import SpawnRequest

    task = {"id": "task", "seat": "eng@fixture", "controller_epoch": setup.held.epoch}
    request = SpawnRequest(setup.config, "eng", setup.store.next_name(setup.config.slug, "eng"), task)
    spill = placement.Spill("worker", "it is draining")
    selected = replace(setup.launcher.select(setup.config, "eng", task), spill=spill)
    admitted = setup.launcher.admit(request, selected)
    order = orders.LaunchOrder(
        setup.config.slug,
        admitted.execution_id,
        admitted.generation,
        "engineer",
        "eng",
        "task",
        "account",
        "claude",
        "subscription",
        "prompt",
        setup.held.epoch,
        lease.now_ms(setup.store) + 60000,
        hive="remote",
    )
    ack = orders.Ack("live", {"host_id": "remote"}, execution={"profile_decision": {"validation": {"pid": 12}}})
    acked = setup.launcher.record(admitted, order, ack)
    assert acked.profile_decision == {"validation": {"pid": 12}, "placement_spill": spill.record()}


def test_a_refused_first_ranked_hive_takes_no_further_launch_in_the_pass(setup, monkeypatch):  # noqa: F811
    write_hive(setup, prefer={"eng": 1})
    write_hive(setup, id="spare")
    launcher = setup.launcher
    launcher.placement_cache = {}
    reserve = launcher.reserve_room
    monkeypatch.setattr(
        launcher,
        "reserve_room",
        lambda request, selected: "remote ram: full" if selected.hive == "remote" else reserve(request, selected),
    )
    request = request_for(setup)
    request.task[ACK_LATER] = True
    assert launcher.spawn(request).status is Status.PENDING
    assert launcher.orders_for(request.task["id"]).hive == "spare"
    assert launcher.select(setup.config, "eng", {"id": "next"}).hive == "spare"


def test_a_local_slot_cap_limits_the_first_room_and_keeps_first_mode():
    from scripts.hive import launch, placement

    hive = {"room": 5, "refusal": "", placement.FIRST_ROOM: 6, placement.FIRST_REFUSAL: ""}
    assert launch._local_capped(hive, 2, 4) == {**hive, "room": 2, placement.FIRST_ROOM: 2}
    full = launch._local_capped(hive, 4, 4)
    assert (full[placement.FIRST_ROOM], full[placement.FIRST_REFUSAL]) == (
        0,
        "max agents: 4 of 4 agents on the hive, fits 0 more",
    )
    assert launch._local_capped({"room": 1, "refusal": ""}, 0, 4) == {"room": 1, "refusal": ""}
