"""The phase pass returns a write per phase checkbox that its applier takes while the done state it read holds."""

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

import scripts.swarm.cli as cli
from scripts.swarm import phases
from scripts.swarm.ledger_client import LedgerRefused
from scripts.tick import engine, view
from scripts.tick.deadline import Deadline
from scripts.tick.ports.records import RevisionConflict
from scripts.tick.step import Context, Domain, Write
from scripts.tick.steps import PASS, actions, tasks

pytestmark = pytest.mark.unit

TICK = phases.PhaseChange("p1", "Build", True, 2, ())
REOPEN = phases.PhaseChange("p2", "Ship", False, 3, ("t3", "t4"))


class Ledger:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.set: list[tuple[Any, ...]] = []

    def hierarchy(self, slug: str) -> list[dict[str, Any]]:
        assert slug == "sw"
        return self.rows

    def set_phase(self, slug: str, phase_id: str, done: bool, status: str) -> None:
        self.set.append((slug, phase_id, done, status))


def node(address: str, depth: int, state: str = "open") -> dict[str, Any]:
    return {"node": address, "kind": address.split("/")[0][:-1], "depth": depth, "state": state}


@pytest.fixture
def held_phases() -> Iterator[list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    token = view.CURRENT.set(view.LedgerView(1, {"phases": rows}))
    kept = view.HELD.set(None)
    yield rows
    view.HELD.reset(kept)
    view.CURRENT.reset(token)


def test_the_phase_step_returns_a_write_per_checkbox_guarded_on_the_done_state_it_read() -> None:
    ledger = Ledger([node("phases/p1", 0), node("tasks/t1", 1, "done"), node("phases/p2", 0), node("tasks/t2", 1)])
    doc = {"phases": [{"id": "p1", "title": "Build"}, {"id": "p2", "title": "Ship", "done": True}]}
    assert tasks.phase_writes("sw", doc, ledger) == [
        Write(tasks.PHASES, "p1", phases.PhaseChange("p1", "Build", True, 1, ()), expected=False),
        Write(tasks.PHASES, "p2", phases.PhaseChange("p2", "Ship", False, 1, ("t2",)), expected=True),
    ]
    assert ledger.set == []


def test_the_phase_step_passes_a_skipped_hierarchy_read_through_as_an_action() -> None:
    class Broken(Ledger):
        def hierarchy(self, slug: str) -> list[dict[str, Any]]:
            raise OSError("refused")

    found = tasks.phase_writes("sw", {"phases": [{"id": "p1", "title": "Build"}]}, Broken([]))
    assert found == ["phase pass skipped, the hierarchy read failed: refused"]


def test_the_phase_applier_sets_the_checkbox_marks_the_box_and_returns_its_action(
    monkeypatch: pytest.MonkeyPatch, held_phases: list[dict[str, Any]]
) -> None:
    taken: list[tuple[Any, ...]] = []

    def apply_change(*args: Any) -> str:
        taken.append(args)
        return "phase p1 ticked"

    monkeypatch.setattr(phases, "apply_change", apply_change)
    held_phases.append({"id": "p1", "done": False})
    box: dict[str, Any] = {}
    writer = tasks.phase_writer("sw", "ledger", "inbox", "store", box)
    assert writer(Write(tasks.PHASES, "p1", TICK, expected=False)) == ["phase p1 ticked"]
    assert taken == [("inbox", "store", "sw", "ledger", TICK)]
    assert box == {tasks.PHASE_CHANGED: True}


def test_the_phase_applier_writes_through_without_a_held_view(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(phases, "apply_change", lambda *args: "phase p2 reopened")
    token = view.CURRENT.set(None)
    try:
        writer = tasks.phase_writer("sw", "ledger", "inbox", "store", {})
        assert writer(Write(tasks.PHASES, "p2", REOPEN, expected=True)) == ["phase p2 reopened"]
    finally:
        view.CURRENT.reset(token)


def test_the_phase_applier_drops_a_checkbox_another_writer_moved(
    monkeypatch: pytest.MonkeyPatch, held_phases: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(phases, "apply_change", lambda *args: pytest.fail("set a moved checkbox"))
    held_phases.append({"id": "p1", "done": True})
    box: dict[str, Any] = {}
    with pytest.raises(RevisionConflict, match="^phase p1 changed its checkbox since the phase pass read it$"):
        tasks.phase_writer("sw", "ledger", "inbox", "store", box)(Write(tasks.PHASES, "p1", TICK, expected=False))
    assert box == {}


def test_the_phase_applier_drops_a_phase_the_held_view_lost(
    monkeypatch: pytest.MonkeyPatch, held_phases: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(phases, "apply_change", lambda *args: pytest.fail("set a lost phase"))
    with pytest.raises(RevisionConflict, match="^phase p2 changed its checkbox"):
        tasks.phase_writer("sw", "ledger", "inbox", "store", {})(Write(tasks.PHASES, "p2", REOPEN, expected=True))


def test_the_phase_applier_drops_a_checkbox_the_ledger_refused(
    monkeypatch: pytest.MonkeyPatch, held_phases: list[dict[str, Any]]
) -> None:
    def refuse(*args: Any) -> str:
        raise LedgerRefused("no")

    monkeypatch.setattr(phases, "apply_change", refuse)
    held_phases.append({"id": "p1"})
    box: dict[str, Any] = {}
    with pytest.raises(RevisionConflict, match="^the ledger refused the checkbox of phase p1$"):
        tasks.phase_writer("sw", "ledger", "inbox", "store", box)(Write(tasks.PHASES, "p1", TICK, expected=False))
    assert box == {}


def test_the_phase_applier_drops_a_write_carrying_no_phase_change() -> None:
    with pytest.raises(RevisionConflict, match="^the phase write of p1 carries no phase change$"):
        tasks.phase_writer("sw", "ledger", "inbox", "store", {})(Write(tasks.PHASES, "p1", "tick", expected=False))


def test_the_phase_applier_is_registered_only_with_an_inbox_and_a_pass_box() -> None:
    state = {"slug": "sw", "ledger": "ledger", "store": "store"}
    assert tasks.PHASES not in tasks.appliers(state)
    assert tasks.PHASES not in tasks.appliers(state | {"inbox": "inbox"})
    assert tasks.PHASES not in tasks.appliers(state | {"box": {}})
    assert tasks.PHASES in tasks.appliers(state | {"inbox": "inbox", "box": {}})


class _State:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state

    def read(self, domain: Domain, key: str) -> object | None:
        return self.state.get(key) if domain == PASS else None


def _phase_pass(ledger: Ledger, doc: dict[str, Any], box: dict[str, Any]) -> list[str]:
    step = tasks.WriteStep(
        "planning and phase pass",
        "phase writes",
        lambda tick, get: tasks.phase_writes(get("slug"), get("doc"), get("ledger")),
        writes=frozenset({tasks.PHASES}),
    )
    ctx = Context(_State({"slug": "sw", "doc": doc, "ledger": ledger}), Deadline.after(60.0))
    writer = tasks.phase_writer("sw", ledger, "inbox", "store", box)
    records = engine.run_group("phase writes", ctx, steps=[step], apply={tasks.PHASES: writer})
    return actions(records)


def test_a_conflicting_checkbox_is_dropped_then_recomputed_on_the_next_pass(
    monkeypatch: pytest.MonkeyPatch, held_phases: list[dict[str, Any]]
) -> None:
    def apply_change(inbox: Any, store: Any, slug: str, ledger: Ledger, change: phases.PhaseChange) -> str:
        ledger.set_phase(slug, change.phase_id, change.done, "")
        return f"phase {change.phase_id} set"

    monkeypatch.setattr(phases, "apply_change", apply_change)
    ledger = Ledger([node("phases/p1", 0), node("tasks/t1", 1, "done")])
    stale = {"phases": [{"id": "p1", "title": "Build"}]}
    held_phases.append({"id": "p1", "done": True})
    box: dict[str, Any] = {}
    first = _phase_pass(ledger, stale, box)
    assert ledger.set == [] and box == {}
    assert any("phase p1 changed its checkbox since the phase pass read it" in line for line in first)
    fresh = {"phases": [{"id": "p1", "title": "Build", "done": True}]}
    assert _phase_pass(ledger, fresh, box) == [] and ledger.set == [] and box == {}
    ledger.rows.append(node("tasks/t2", 1))
    assert _phase_pass(ledger, fresh, box) == ["phase p1 set"]
    assert ledger.set == [("sw", "p1", False, "")] and box == {tasks.PHASE_CHANGED: True}


def test_planning_runs_again_after_the_phase_pass_only_when_a_checkbox_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[tuple[Any, ...]] = []
    store = SimpleNamespace(config=lambda slug: f"config of {slug}")
    monkeypatch.setattr(cli.timing, "call", lambda function, *args: function(*args))
    monkeypatch.setattr(cli.ledger_client, "pass_state", lambda ledger, slug: {"read": slug})
    monkeypatch.setattr(cli.phase_planning, "planning_pass", lambda *args: ran.append(args) or ["queued plan"])
    box: dict[str, Any] = {}
    assert cli._planning_after_phase("inbox", store, "sw", "ledger", box) == [] and ran == []
    box[tasks.PHASE_CHANGED] = True
    assert cli._planning_after_phase("inbox", store, "sw", "ledger", box) == ["queued plan"]
    assert ran == [("inbox", store, "sw", {"read": "sw"}, "ledger", "config of sw")] and box == {}
