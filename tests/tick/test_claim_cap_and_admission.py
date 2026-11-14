"""Direct coverage of place._settle_admission's gate activation and _claim_cap's unblocked-update branch, plus
common._settle_exit's coordination-port path, restored from the deleted spawn-step test suite."""

from types import SimpleNamespace
from typing import Any, cast

from scripts.swarm.store import AgentRecord
from scripts.tick.adapters.calls import AdmissionCalls, SpawnStoreCalls
from scripts.tick.adapters.fakes import FakeCoordination, FakeInbox, FakeLedger
from scripts.tick.deadline import Deadline
from scripts.tick.steps import common, place

DEADLINE = Deadline(10, lambda: 0)


class _AdmissionPort:
    def __init__(self, reservation: str) -> None:
        self.reservation = reservation
        self.activated: list[tuple[Any, ...]] = []

    def held_admission(self, slug: str, task_id: str, deadline: Deadline) -> str:
        return self.reservation

    def activate_admission(self, slug: str, task_id: str, reservation: str, deadline: Deadline) -> bool:
        self.activated.append((slug, task_id, reservation))
        return True


def test_settle_admission_activates_a_held_reservation() -> None:
    port = _AdmissionPort("res-1")
    runtime = SimpleNamespace(admission=AdmissionCalls(cast(Any, port), DEADLINE))
    agent = SimpleNamespace(task="t1")

    place._settle_admission("sw", runtime, agent)

    assert port.activated == [("sw", "t1", "res-1")]


def test_claim_cap_returns_blank_when_the_guarded_update_leaves_the_task_not_blocked() -> None:
    store = SimpleNamespace(
        claims=lambda slug, task_id: place.claim_cap.CAP,
        config=lambda slug: SimpleNamespace(gates={}),
        handoff_envelope=lambda slug, task_id: {"reason": "quota"},
        launches=lambda slug: [],
    )
    ledger = SimpleNamespace(update_task=lambda slug, task_id, patch, if_state: {"state": "claimed"})
    rows: dict[str, Any] = {"t1": {"id": "t1", "state": "open"}}

    said = place._claim_cap("sw", store, ledger, rows, {"id": "t1"})

    assert said == ""
    assert rows["t1"]["state"] == "claimed"


def test_a_dropped_spawn_claim_releases_through_coordination_and_settles_through_the_inbox() -> None:
    port, inbox = FakeCoordination(), FakeInbox()
    store = SpawnStoreCalls(port, DEADLINE, inbox)
    agent = AgentRecord("eng-1", "eng", "t1", seat="eng-1@sw")

    assert common._drop("sw", store, FakeLedger(), {}, agent) == ""

    seen = [(call.operation, call.args, call.deadline) for call in port.calls]
    assert seen == [
        ("store_read", ("claim_generation", ("sw", "t1"), {}), DEADLINE),
        ("store_write", ("release", ("sw", "t1", "eng-1"), {}), DEADLINE),
        ("store_write", ("drop_agent", ("sw", "eng-1"), {}), DEADLINE),
    ]
    assert [(call.operation, call.args, call.deadline) for call in inbox.calls] == [
        ("settle_exit", ("eng-1", "", "stopped"), DEADLINE)
    ]
