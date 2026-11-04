"""The master stall step returns a write per stalled master that its applier takes while that master stays live."""

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

import scripts.swarm.cli  # noqa: F401
from scripts.swarm import affinity, master_retire, master_stall
from scripts.swarm.store import MASTER, AgentRecord
from scripts.tick import engine
from scripts.tick.deadline import Deadline
from scripts.tick.ports.records import RevisionConflict
from scripts.tick.step import Context, Domain, Write
from scripts.tick.steps import PASS, actions, masters, tasks

pytestmark = pytest.mark.unit

FIRST = AgentRecord("master-1", MASTER, MASTER, seat="master@sw")
SECOND = AgentRecord("master-2", MASTER, MASTER, seat="master@sw")
RUNNING = SimpleNamespace(state="running")
HELD = "busy with no tool call for 12 minutes"


class Runtime:
    def __init__(self, *live: str) -> None:
        self.live = set(live)

    def live_names(self) -> set[str]:
        return self.live


def fail(*args: Any) -> Any:
    pytest.fail(f"the stall pass wrote: {args}")


@pytest.fixture
def quiet(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    found: dict[str, Any] = {"live": [FIRST], "minutes": 12, "held": "", "blocked": False}

    def live_master(store: Any, slug: str) -> AgentRecord | None:
        assert slug == "sw"
        masters_left = found["live"]
        return masters_left.pop(0) if len(masters_left) > 1 else masters_left[0]

    monkeypatch.setattr(affinity, "live_master", live_master)
    monkeypatch.setattr(master_stall, "quiet_minutes", lambda store, slug, master, now_ms: found["minutes"])
    monkeypatch.setattr(master_stall, "parent_blocked", lambda slug, store, ledger: found["blocked"])
    monkeypatch.setattr(master_retire, "reason", lambda store, slug, name: found["held"])
    monkeypatch.setattr(master_stall.health, "limits", lambda: SimpleNamespace(stalled_minutes=10))
    for name in ("forget", "hold"):
        monkeypatch.setattr(master_retire, name, fail)
    monkeypatch.setattr(master_stall, "recycle", fail)
    yield found


def test_the_stall_pass_decides_a_retire_for_a_quiet_master_writing_nothing(quiet: dict[str, Any]) -> None:
    found = master_stall.stalls("sw", RUNNING, "store", "ledger", Runtime("master-1"), 7)
    assert found == [master_stall.Stall(FIRST, master_stall.RETIRE, 12, "", True)]


def test_the_stall_pass_decides_a_retire_for_a_master_whose_pane_is_gone(quiet: dict[str, Any]) -> None:
    quiet["held"] = HELD
    found = master_stall.stalls("sw", RUNNING, "store", "ledger", Runtime(), 7)
    assert found == [master_stall.Stall(FIRST, master_stall.RETIRE, 12, HELD, False)]


def test_the_stall_pass_decides_a_forget_for_a_held_master_that_resumed(quiet: dict[str, Any]) -> None:
    quiet |= {"minutes": 3, "held": HELD}
    found = master_stall.stalls("sw", RUNNING, "store", "ledger", Runtime("master-1"), 7)
    assert found == [master_stall.Stall(FIRST, master_stall.FORGET, 0, HELD, False)]


@pytest.mark.parametrize("minutes", [None, 3, 9])
def test_the_stall_pass_decides_nothing_for_a_master_under_the_limit(quiet: dict[str, Any], minutes: Any) -> None:
    quiet |= {"minutes": minutes, "held": "asked for a handoff"}
    assert master_stall.stalls("sw", RUNNING, "store", "ledger", Runtime("master-1"), 7) == []


def test_the_stall_pass_decides_a_stop_for_a_proof_swarm_whose_parent_is_blocked(quiet: dict[str, Any]) -> None:
    quiet["blocked"] = True
    found = master_stall.stalls("sw", RUNNING, "store", "ledger", Runtime("master-1"), 7)
    assert found == [master_stall.Stall(FIRST, master_stall.STOP, 12, "", False)]


@pytest.mark.parametrize("state", ["stopping", "stopped"])
def test_the_stall_pass_decides_nothing_for_a_stopping_swarm(quiet: dict[str, Any], state: str) -> None:
    assert master_stall.stalls("sw", SimpleNamespace(state=state), "store", "ledger", Runtime(), 7) == []


def test_the_stall_pass_decides_nothing_without_a_live_master_lead(quiet: dict[str, Any]) -> None:
    quiet["live"] = [AgentRecord("eng-1", "eng", "t1")]
    assert master_stall.stalls("sw", RUNNING, "store", "ledger", Runtime(), 7) == []
    quiet["live"] = [None]
    assert master_stall.stalls("sw", RUNNING, "store", "ledger", Runtime(), 7) == []


def test_the_stall_step_returns_a_write_per_stall_guarded_on_the_master_it_read() -> None:
    stall = master_stall.Stall(FIRST, master_stall.RETIRE, 12, "", True)
    found = tasks.stall_writes(["skipped the stall pass", stall])
    assert found == ["skipped the stall pass", Write(tasks.STALL, "master-1", stall, expected="master-1")]


def test_the_stall_applier_takes_the_stall_while_its_master_stays_live(
    monkeypatch: pytest.MonkeyPatch, quiet: dict[str, Any]
) -> None:
    taken: list[tuple[Any, ...]] = []

    def apply_stall(slug: str, store: Any, ledger: Any, stall: master_stall.Stall, now_ms: int) -> list[str]:
        taken.append((slug, store, ledger, stall, now_ms))
        return ["recycled master master-1"]

    monkeypatch.setattr(master_stall, "apply_stall", apply_stall)
    stall = master_stall.Stall(FIRST, master_stall.RETIRE, 12, "", True)
    writer = tasks.stall_writer("sw", "store", "ledger", 7)
    assert writer(Write(tasks.STALL, "master-1", stall, expected="master-1")) == ["recycled master master-1"]
    assert taken == [("sw", "store", "ledger", stall, 7)]


def test_the_stall_applier_drops_a_stall_whose_master_was_replaced(quiet: dict[str, Any]) -> None:
    quiet["live"] = [SECOND]
    stall = master_stall.Stall(FIRST, master_stall.RETIRE, 12, "", True)
    with pytest.raises(RevisionConflict) as raised:
        tasks.stall_writer("sw", "store", "ledger", 7)(Write(tasks.STALL, "master-1", stall, expected="master-1"))
    assert str(raised.value) == "master master-1 is no longer the live master of swarm sw"


def test_the_stall_applier_drops_a_stall_once_no_master_is_live(quiet: dict[str, Any]) -> None:
    quiet["live"] = [None]
    stall = master_stall.Stall(FIRST, master_stall.FORGET, 0, HELD, False)
    with pytest.raises(RevisionConflict) as raised:
        tasks.stall_writer("sw", "store", "ledger", 7)(Write(tasks.STALL, "master-1", stall, expected="master-1"))
    assert str(raised.value) == "master master-1 is no longer the live master of swarm sw"


def test_the_stall_applier_drops_a_write_carrying_no_stall() -> None:
    with pytest.raises(RevisionConflict) as raised:
        tasks.stall_writer("sw", "store", "ledger", 7)(Write(tasks.STALL, "master-1", "x", expected="master-1"))
    assert str(raised.value) == "the stall write of master-1 carries no stall"


def test_the_stall_applier_is_registered_with_a_store_and_a_pass_time() -> None:
    state = {"slug": "sw", "ledger": "ledger", "store": "store"}
    assert tasks.STALL not in tasks.appliers(state)
    assert tasks.STALL not in tasks.appliers({"slug": "sw", "ledger": "ledger", "now_ms": 7})
    assert tasks.STALL in tasks.appliers(state | {"now_ms": 7})


def test_the_master_stall_step_writes_the_stall_domain() -> None:
    step = next(step for step in masters.STEPS if step.name == "master stall")
    assert step.writes == frozenset({tasks.STALL})


def test_apply_stall_forgets_the_held_reason_of_a_master_that_resumed(monkeypatch: pytest.MonkeyPatch) -> None:
    forgot: list[tuple[Any, ...]] = []
    monkeypatch.setattr(master_retire, "forget", lambda *args: forgot.append(args) or True)
    stall = master_stall.Stall(FIRST, master_stall.FORGET, 0, HELD, False)
    assert master_stall.apply_stall("sw", "store", "ledger", stall, 7) == []
    assert forgot == [("store", "sw", "master-1", master_stall.RESUMED)]


def test_apply_stall_stops_a_proof_swarm_whose_parent_is_blocked() -> None:
    updated: list[tuple[str, dict[str, Any]]] = []
    store = SimpleNamespace(update=lambda slug, **fields: updated.append((slug, fields)))
    stall = master_stall.Stall(FIRST, master_stall.STOP, 12, "", False)
    assert master_stall.apply_stall("sw", store, "ledger", stall, 7) == [
        "stopped proof swarm sw: its parent task is blocked and its master master-1 stalled"
    ]
    assert updated == [("sw", {"state": "stopping"})]


def test_apply_stall_holds_a_live_master_for_its_handoff(monkeypatch: pytest.MonkeyPatch) -> None:
    held: list[tuple[Any, ...]] = []
    monkeypatch.setattr(master_retire, "hold", lambda *args: held.append(args) or "asked master-1 for a handoff")
    monkeypatch.setattr(master_stall, "recycle", fail)
    stall = master_stall.Stall(FIRST, master_stall.RETIRE, 12, "", True)
    assert master_stall.apply_stall("sw", "store", "ledger", stall, 7) == ["asked master-1 for a handoff"]
    assert held == [("store", "sw", FIRST, "busy with no tool call for 12 minutes", True, 7)]


def test_apply_stall_keeps_the_held_reason_it_asked_with(monkeypatch: pytest.MonkeyPatch) -> None:
    held: list[tuple[Any, ...]] = []
    monkeypatch.setattr(master_retire, "hold", lambda *args: held.append(args) or "waiting")
    stall = master_stall.Stall(FIRST, master_stall.RETIRE, 15, HELD, False)
    assert master_stall.apply_stall("sw", "store", "ledger", stall, 7) == ["waiting"]
    assert held == [("store", "sw", FIRST, HELD, False, 7)]


def test_apply_stall_recycles_a_master_once_its_hold_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    recycled: list[tuple[Any, ...]] = []
    monkeypatch.setattr(master_retire, "hold", lambda *args: "")
    monkeypatch.setattr(master_stall, "recycle", lambda *args: recycled.append(args))
    stall = master_stall.Stall(FIRST, master_stall.RETIRE, 15, HELD, True)
    assert master_stall.apply_stall("sw", "store", "ledger", stall, 9) == [
        "recycled master master-1 in its seat after 15 minutes busy with no tool call"
    ]
    text = master_stall.RECOVERY.format(slug="sw", name="master-1", minutes=15)
    assert recycled == [("sw", "store", "ledger", FIRST, text, 9, master_stall.STALLED)]


class _State:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state

    def read(self, domain: Domain, key: str) -> object | None:
        return self.state.get(key) if domain == PASS else None


def _stall_pass() -> list[str]:
    step = next(step for step in masters.STEPS if step.name == "master stall")
    state = {"slug": "sw", "config": RUNNING, "store": "store", "ledger": "ledger", "runtime": Runtime(), "now_ms": 7}
    state["box"] = {}
    ctx = Context(_State(state), Deadline.after(60.0))
    records = engine.run_group(
        "stall writes", ctx, steps=[step], apply={tasks.STALL: tasks.stall_writer("sw", "store", "ledger", 7)}
    )
    return actions(records)


def test_a_stall_of_a_replaced_master_is_dropped_then_recomputed_on_the_next_pass(
    monkeypatch: pytest.MonkeyPatch, quiet: dict[str, Any]
) -> None:
    taken: list[str] = []

    def apply_stall(slug: str, store: Any, ledger: Any, stall: master_stall.Stall, now_ms: int) -> list[str]:
        taken.append(stall.master.name)
        return [f"took the stall of {stall.master.name}"]

    monkeypatch.setattr(master_stall, "apply_stall", apply_stall)
    quiet["live"] = [FIRST, SECOND]
    first = _stall_pass()
    assert taken == []
    assert any("master master-1 is no longer the live master of swarm sw" in line for line in first)
    assert _stall_pass() == ["took the stall of master-2"]
    assert taken == ["master-2"]
