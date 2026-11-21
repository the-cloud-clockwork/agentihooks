"""The thin claim hold: an eng or ci task the thin check marked for enhancement waits for its enhancement plan task."""

from typing import Any

import pytest

from scripts.swarm import claim_hold, readiness
from scripts.swarm_ledger.repository import hierarchy

pytestmark = pytest.mark.unit

HELD = "it waits for its enhancement plan task enhance-a"


def task(task_id: str, **fields: Any) -> dict[str, Any]:
    return {"id": task_id, "title": f"title {task_id}", "phase": "p1", "state": "open", "lane": "eng", **fields}


def doc(*tasks: dict[str, Any]) -> dict[str, Any]:
    return {"overview": "o", "tasks": list(tasks), "phases": [{"id": "p1", "title": "build", "depends_on": []}]}


def view(found: dict[str, Any]) -> readiness.Snapshot:
    rows = {t["id"]: t for t in found["tasks"]}
    return readiness.Snapshot("sw", rows, found, hierarchy.project(found)[0], "", lambda task_id: False, lambda t: True)


def reasons(found: dict[str, Any], task_id: str) -> tuple[str, bool, str]:
    seen = view(found)
    row = seen.rows[task_id]
    return (
        claim_hold.hold(row, seen.rows, seen.doc, seen.graph, ""),
        claim_hold.holds(row, seen.rows, seen.doc, seen.graph, ""),
        claim_hold.urgent(row, seen.rows, seen.doc, seen.graph, ""),
    )


@pytest.mark.feature("swarm-thin-hold")
@pytest.mark.parametrize(
    "fields",
    [
        {"plan_slice": "thin-check"},
        {"enhancement": ""},
        {"enhancement": "planned"},
        {"enhancement": "skipped"},
        {"lane": "ci", "enhancement": "planned"},
    ],
)
def test_a_sliced_planned_skipped_or_unchecked_task_stays_claimable(fields: dict[str, Any]) -> None:
    found = doc(task("a", **fields))
    lane = fields.get("lane", "eng")
    assert reasons(found, "a") == ("", False, "")
    assert readiness.claimable(found["tasks"][0], lane, view(found)) is True


@pytest.mark.feature("swarm-thin-hold")
def test_a_plan_lane_task_needing_enhancement_stays_claimable_by_planners() -> None:
    found = doc(task("a", lane="plan", kind="plan", enhancement="needed"))
    found["phases"][0]["planning"] = "auto"
    assert reasons(found, "a") == ("", False, "")
    assert readiness.claimable(found["tasks"][0], "plan", view(found)) is True


@pytest.mark.feature("swarm-thin-hold")
def test_a_sliced_task_still_needing_enhancement_stays_held_until_marked_planned() -> None:
    found = doc(task("a", enhancement="needed", plan_slice="thin-check"))
    assert reasons(found, "a") == (HELD, True, HELD)
    assert readiness.claimable(found["tasks"][0], "eng", view(found)) is False
    planned = doc(task("a", enhancement="planned", plan_slice="thin-check"))
    assert readiness.claimable(planned["tasks"][0], "eng", view(planned)) is True


@pytest.mark.feature("swarm-thin-hold")
@pytest.mark.parametrize("lane", ["eng", "ci"])
def test_an_eng_or_ci_task_needing_enhancement_is_held_from_its_lane_claim(lane: str) -> None:
    found = doc(task("a", lane=lane, enhancement="needed"), task("b", lane=lane))
    assert reasons(found, "a") == (HELD, True, HELD)
    assert readiness.claimable(found["tasks"][0], lane, view(found)) is False
    assert readiness.claimable(found["tasks"][1], lane, view(found)) is True


@pytest.mark.feature("swarm-thin-hold")
def test_a_task_without_a_lane_reads_as_eng_for_the_thin_hold() -> None:
    bare = {k: v for k, v in task("a", enhancement="needed").items() if k != "lane"}
    assert reasons(doc(bare), "a") == (HELD, True, HELD)


@pytest.mark.feature("swarm-thin-hold")
def test_the_thin_hold_names_the_enhancement_task_of_its_own_target() -> None:
    assert claim_hold.thin(task("t-x", enhancement="needed")) == "it waits for its enhancement plan task enhance-t-x"
    assert claim_hold.thin(task("t-x", enhancement="planned")) == ""


@pytest.mark.feature("swarm-thin-hold")
def test_an_earlier_hold_still_names_itself_before_the_thin_hold() -> None:
    found = doc(task("dep"), task("a", enhancement="needed", depends_on=["dep"]))
    assert reasons(found, "a") == ("it waits on task dep", True, "it waits on task dep")
