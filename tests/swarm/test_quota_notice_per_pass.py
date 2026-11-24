"""The quota notice step's per pass work: one focus queue per readiness sweep, one profile lookup per profile."""

from pathlib import Path
from typing import Any

import pytest

from scripts.swarm import claim_hold, dispatcher, freeze, readiness
from scripts.swarm import runtime as runtime_module
from scripts.swarm.store import SwarmConfig
from scripts.swarm_ledger.repository import hierarchy

pytestmark = pytest.mark.unit

PLANS = [{"id": "a", "title": "Swarm v2"}]
PHASES = [{"id": "p1", "title": "Build", "plan": "plans/a"}]
FOCUS = {"id": "focus-f", "verb": "focus", "target": "tasks/f", "by": "operator", "at": 1, "reason": ""}


def task(task_id: str, **fields: Any) -> dict[str, Any]:
    return {"id": task_id, "lane": "eng", "state": "open", "phase": "p1", **fields}


def ledger(unfocused: int) -> dict[str, Any]:
    rows = [task("d", state="done"), task("f", depends_on=["d"])]
    rows += [task(f"t{n}", depends_on=["d"]) for n in range(unfocused)]
    return {"plans": PLANS, "phases": PHASES, "slices": [], "tasks": rows, "freezes": [FOCUS]}


def snapshot(found: dict[str, Any]) -> readiness.Snapshot:
    rows = {row["id"]: row for row in found["tasks"]}
    graph = hierarchy.project(found)[0]
    return readiness.Snapshot("sw", rows, found, graph, "", lambda task_id: False, lambda row: True)


def counted_admitted(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    real = freeze.admitted

    def counting(*args: Any) -> frozenset[str] | None:
        calls.append(1)
        return real(*args)

    monkeypatch.setattr(freeze, "admitted", counting)
    return calls


def counted_profiles(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    def claude_only(profile: str) -> bool:
        seen.append(profile)
        return profile == "frontend"

    monkeypatch.setattr(runtime_module.plugins, "claude_only", claude_only)
    return seen


@pytest.mark.feature("quota-notice-per-pass-work")
def test_the_dispatcher_guard_keeps_the_focused_chain_and_drops_held_work() -> None:
    assert dispatcher.guarded(ledger(5), "") == {"f"}


@pytest.mark.feature("quota-notice-per-pass-work")
def test_the_dispatcher_guard_computes_the_focus_queue_once_whatever_the_open_task_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = counted_admitted(monkeypatch)
    assert dispatcher.guarded(ledger(30), "") == {"f"}
    assert len(calls) == 1


@pytest.mark.feature("quota-notice-per-pass-work")
def test_claimable_over_a_snapshot_agrees_with_ready_for_every_task() -> None:
    found = ledger(5)
    view = snapshot(found)
    claimable = {row["id"]: readiness.claimable(row, "eng", view) for row in found["tasks"]}
    reference = {row["id"]: readiness.ready(row, view.rows, found, view.graph, "") for row in found["tasks"]}
    assert (
        claimable
        == reference
        == {"d": False, "f": True, "t0": False, "t1": False, "t2": False, "t3": False, "t4": False}
    )


@pytest.mark.feature("quota-notice-per-pass-work")
def test_claimable_computes_the_focus_queue_once_per_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    found = ledger(30)
    view = snapshot(found)
    calls = counted_admitted(monkeypatch)
    ready = [row["id"] for lane in ("eng", "ci") for row in found["tasks"] if readiness.claimable(row, lane, view)]
    assert ready == ["f"]
    assert len(calls) == 1


@pytest.mark.feature("quota-notice-per-pass-work")
def test_claim_hold_uses_the_focus_queue_handed_in(monkeypatch: pytest.MonkeyPatch) -> None:
    found = ledger(5)
    view = snapshot(found)
    queue = freeze.admitted(found, view.graph)
    calls = counted_admitted(monkeypatch)
    open_rows = [row for row in found["tasks"] if row["state"] == "open"]
    held = {row["id"]: claim_hold.holds(row, view.rows, found, view.graph, "", queue) for row in open_rows}
    assert held == {"f": False, "t0": True, "t1": True, "t2": True, "t3": True, "t4": True}
    assert calls == []


@pytest.mark.feature("quota-notice-per-pass-work")
def test_quota_requirements_keep_each_profile_answer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    counted_profiles(monkeypatch)
    runtime = runtime_module.HerdrRuntime(home=tmp_path)
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0)
    ready = {"eng": [task("a", profile="frontend"), task("b", profile="engineer"), task("c", profile="frontend")]}
    options = runtime.quota_requirements(config, {**ready, "ci": [], "plan": []})["eng"]
    assert [option == ("claude",) for option in options] == [True, False, True]


@pytest.mark.feature("quota-notice-per-pass-work")
def test_quota_requirements_look_up_each_profile_once_per_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = counted_profiles(monkeypatch)
    runtime = runtime_module.HerdrRuntime(home=tmp_path)
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0)
    tasks = [task(f"t{n}", profile=("frontend", "engineer")[n % 2]) for n in range(20)]
    runtime.quota_requirements(config, {"eng": tasks, "ci": [], "plan": []})
    runtime.quota_requirements(config, {"eng": tasks, "ci": [], "plan": []})
    assert sorted(seen) == ["engineer", "engineer", "frontend", "frontend"]
