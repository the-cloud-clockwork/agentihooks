"""The phase projection lists each phase's tasks and their states from the pass document with no ledger read."""

import pytest

from scripts.swarm import phase_projection

pytestmark = pytest.mark.unit


def test_the_projection_lists_each_phase_s_tasks_through_slices_and_skips_tasks_outside_any_phase() -> None:
    doc = {
        "plans": [{"id": "a"}],
        "phases": [{"id": "p1", "plan": "plans/a"}, {"id": "p2"}],
        "slices": [{"id": "s1", "phase": "phases/p1"}, {"id": "s2", "phase": "plans/a"}],
        "tasks": [
            {"id": "t1", "slice": "slices/s1", "state": "done"},
            {"id": "t2", "phase": "p1"},
            {"id": "t3", "phase": "p1", "done": True},
            {"id": "t4", "phase": "p2", "state": "claimed", "done": True},
            {"id": "t5", "phase": "p1", "state": "done", "out_of_scope": True},
            {"id": "t6", "phase": "missing", "state": "open"},
            {"id": "t7", "state": "open"},
            {"id": "t8", "slice": "slices/gone", "state": "open"},
            {"id": "t9", "slice": "slices/s2", "state": "open"},
        ],
    }
    assert phase_projection.projected_phase_tasks(doc) == {
        "p1": [("t1", "done"), ("t2", "open"), ("t3", "done"), ("t5", "out_of_scope")],
        "p2": [("t4", "claimed")],
    }


def test_the_projection_of_a_document_without_tasks_lists_each_phase_empty() -> None:
    assert phase_projection.projected_phase_tasks({"phases": [{"id": "p1"}]}) == {"p1": []}
    assert phase_projection.projected_phase_tasks({}) == {}


@pytest.mark.timeout(5)
def test_the_projection_stops_at_a_slice_that_names_itself_as_its_phase() -> None:
    doc = {
        "phases": [{"id": "p1"}],
        "slices": [{"id": "s1", "phase": "slices/s2"}, {"id": "s2", "phase": "slices/s1"}],
        "tasks": [{"id": "t1", "slice": "slices/s1", "state": "open"}],
    }
    assert phase_projection.projected_phase_tasks(doc) == {"p1": []}
