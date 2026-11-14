"""The place, admission and placement-pass operations the runtime port serves: the router adapter, the fake."""

from types import SimpleNamespace
from typing import Any, cast

import pytest

from scripts.swarm.store import AgentRecord
from scripts.tick.adapters.calls import AdmissionCalls
from scripts.tick.adapters.fakes import FakeRuntime
from scripts.tick.adapters.runtime import RouterRuntime
from scripts.tick.deadline import Deadline

pytestmark = pytest.mark.unit

EXPIRED = Deadline(0, lambda: 0)
AGENT = AgentRecord("eng-1", "eng", "t1")
CONFIG = cast(Any, SimpleNamespace(slug="sw"))
TASK = {"id": "t1"}


def test_the_fake_runtime_records_place_capacity_and_placement_pass_calls() -> None:
    deadline = Deadline(10, lambda: 0)
    port = FakeRuntime(
        answers={
            "place": "placed",
            "has_capacity": True,
            "launch_room": 3,
            "reported": True,
            "placement_refusal": "full",
            "placement": "kubernetes",
            "reap_name": True,
            "admits": True,
        }
    )

    assert port.place(CONFIG, "eng", "eng-1", TASK, deadline=deadline) == "placed"
    assert port.has_capacity(CONFIG, deadline=deadline) is True
    assert port.launch_room(CONFIG, deadline=deadline) == 3
    assert port.reported(AGENT, deadline=deadline) is True
    assert port.placement_refusal("eng", TASK, deadline=deadline) == "full"
    assert port.placement(deadline=deadline) == "kubernetes"
    port.begin_placement(deadline=deadline)
    port.end_placement(deadline=deadline)
    assert port.reap_name("eng-1", deadline=deadline) is True
    assert port.admits(deadline=deadline) is True

    assert [(call.operation, call.args, call.deadline) for call in port.calls] == [
        ("place", (CONFIG, "eng", "eng-1", TASK), deadline),
        ("has_capacity", (CONFIG,), deadline),
        ("launch_room", (CONFIG,), deadline),
        ("reported", (AGENT,), deadline),
        ("placement_refusal", ("eng", TASK), deadline),
        ("placement", (), deadline),
        ("begin_placement", (), deadline),
        ("end_placement", (), deadline),
        ("reap_name", ("eng-1",), deadline),
        ("admits", (), deadline),
    ]


def test_admission_calls_admits_and_releases_through_the_runtime_port() -> None:
    deadline = Deadline(10, lambda: 0)
    port = FakeRuntime(answers={"admit": ["decision"], "held_admission": "r1", "activate_admission": True})
    gate = AdmissionCalls(port, deadline)

    assert gate.admit("sw", [TASK], 7) == ["decision"]
    gate.release("sw", "t1", "r1")
    assert gate.held("sw", "t1") == "r1"
    assert gate.activate("sw", "t1", "r1") is True

    assert [(call.operation, call.args, call.deadline) for call in port.calls] == [
        ("admit", ("sw", [TASK], 7), deadline),
        ("release_admission", ("sw", "t1", "r1"), deadline),
        ("held_admission", ("sw", "t1"), deadline),
        ("activate_admission", ("sw", "t1", "r1"), deadline),
    ]


class _Legacy:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.admission = SimpleNamespace(
            admit=lambda *args: self.calls.append(("admit", *args)) or ["decision"],
            release=lambda *args: self.calls.append(("release", *args)),
            held=lambda *args: self.calls.append(("held", *args)) or "r1",
            activate=lambda *args: self.calls.append(("activate", *args)) or True,
        )
        self.router = SimpleNamespace(placement="hive")

    def spawn(self, *args: object) -> str:
        self.calls.append(("spawn", *args))
        return "placed"

    def has_capacity(self, config: object) -> bool:
        self.calls.append(("has_capacity", config))
        return False

    def launch_room(self, config: object) -> int:
        self.calls.append(("launch_room", config))
        return 2

    def reported(self, agent: object) -> bool:
        self.calls.append(("reported", agent))
        return True

    def placement_refusal(self, lane: str, task: object) -> str:
        self.calls.append(("placement_refusal", lane, task))
        return "elsewhere"

    def begin_placement_pass(self) -> None:
        self.calls.append(("begin",))

    def end_placement_pass(self) -> None:
        self.calls.append(("end",))

    def reap_name(self, name: str) -> bool:
        self.calls.append(("reap_name", name))
        return True


def _router(legacy: object) -> RouterRuntime:
    return RouterRuntime(cast(Any, None), lambda name: None, legacy=legacy)


def test_the_router_runtime_forwards_spawn_operations_to_the_pass_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    legacy, deadline = _Legacy(), Deadline(10, lambda: 0)
    monkeypatch.setattr("scripts.tick.adapters.runtime.PendingAdmission", SimpleNamespace, raising=False)
    adapter = _router(legacy)

    assert adapter.place(CONFIG, "eng", "eng-1", TASK, deadline=deadline) == "placed"
    assert adapter.has_capacity(CONFIG, deadline=deadline) is False
    assert adapter.launch_room(CONFIG, deadline=deadline) == 2
    assert adapter.reported(AGENT, deadline=deadline) is True
    assert adapter.placement_refusal("eng", TASK, deadline=deadline) == "elsewhere"
    adapter.begin_placement(deadline=deadline)
    adapter.end_placement(deadline=deadline)
    assert adapter.reap_name("eng-1", deadline=deadline) is True
    assert adapter.placement(deadline=deadline) == "hive"
    assert adapter.admits(deadline=deadline) is True
    assert adapter.admit("sw", [TASK], 7, deadline=deadline) == ["decision"]
    adapter.release_admission("sw", "t1", "r1", deadline=deadline)
    assert adapter.held_admission("sw", "t1", deadline=deadline) == "r1"
    assert adapter.activate_admission("sw", "t1", "r1", deadline=deadline) is True

    assert legacy.calls == [
        ("spawn", CONFIG, "eng", "eng-1", TASK),
        ("has_capacity", CONFIG),
        ("launch_room", CONFIG),
        ("reported", AGENT),
        ("placement_refusal", "eng", TASK),
        ("begin",),
        ("end",),
        ("reap_name", "eng-1"),
        ("admit", "sw", [TASK], 7),
        ("release", "sw", "t1", "r1"),
        ("held", "sw", "t1"),
        ("activate", "sw", "t1", "r1"),
    ]


def test_the_router_runtime_keeps_the_optional_runtime_defaults() -> None:
    deadline = Deadline(10, lambda: 0)
    adapter = _router(SimpleNamespace(admission=object()))

    assert adapter.launch_room(CONFIG, deadline=deadline) is None
    assert adapter.placement_refusal("eng", TASK, deadline=deadline) == ""
    assert adapter.begin_placement(deadline=deadline) is None
    assert adapter.end_placement(deadline=deadline) is None
    assert adapter.placement(deadline=deadline) is None
    assert adapter.admits(deadline=deadline) is False


def test_the_router_runtime_answers_no_launch_room_when_the_pass_runtime_names_none() -> None:
    adapter = _router(SimpleNamespace(launch_room=lambda config: None))

    assert adapter.launch_room(CONFIG, deadline=Deadline(10, lambda: 0)) is None


def test_the_router_runtime_refuses_spawn_operations_past_their_deadline() -> None:
    adapter = _router(_Legacy())

    with pytest.raises(TimeoutError) as placing:
        adapter.place(CONFIG, "eng", "eng-1", TASK, deadline=EXPIRED)
    with pytest.raises(TimeoutError) as admitting:
        adapter.admit("sw", [TASK], 7, deadline=EXPIRED)

    assert [str(placing.value), str(admitting.value)] == ["tool deadline expired"] * 2
