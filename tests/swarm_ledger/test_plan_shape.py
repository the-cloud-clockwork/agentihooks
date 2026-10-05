import json
from pathlib import Path

import pytest

from scripts.swarm_ledger.plan_shape import analyze

pytestmark = pytest.mark.unit


def test_recorded_plan_reports_the_longest_chain_and_parallel_width():
    ledger = json.loads((Path(__file__).parents[1] / "fixtures/rig_doctor_plan.json").read_text())
    shape = analyze(ledger["tasks"])
    assert shape["critical_path"] == ["dt1", "ds1", "ds2", "sk1", "ci1", "lp1"]
    assert shape["chain_length"] == 6
    assert shape["parallel_width"] == 6
    assert shape["engineer_width"] == 6


@pytest.mark.parametrize(
    ("tasks", "length", "width", "engineers"),
    [
        ([], 0, 0, 0),
        ([{"id": "a"}, {"id": "b"}], 1, 2, 2),
        ([{"id": "a"}, {"id": "b", "depends_on": ["a"]}], 2, 1, 1),
        (
            [
                {"id": "a"},
                {"id": "b", "lane": "ci"},
                {"id": "c", "depends_on": ["b"]},
                {"id": "d", "depends_on": ["b"]},
            ],
            2,
            3,
            3,
        ),
        ([{"id": "a"}, {"id": "b", "lane": "ci", "depends_on": ["a"]}, {"id": "c", "depends_on": ["b"]}], 3, 1, 1),
        ([{"id": "a", "done": True}, {"id": "b", "depends_on": ["a"]}, {"id": "c", "out_of_scope": True}], 1, 1, 1),
        ([{"id": "a", "out_of_scope": True}, {"id": "b", "depends_on": ["a"]}], 1, 1, 1),
    ],
)
def test_dependency_width_is_the_maximum_antichain_across_levels(tasks, length, width, engineers):
    shape = analyze(tasks)
    assert (shape["chain_length"], shape["parallel_width"], shape["engineer_width"]) == (length, width, engineers)


def test_seven_deep_two_wide_plan_warns_below_four_engineers():
    from scripts.swarm_ledger.plan_shape import report

    tasks = [{"id": str(n), "depends_on": [str(n - 1)] if n else []} for n in range(7)] + [{"id": "independent"}]
    shape = report(tasks, 4)
    assert shape["critical_path"] == ["0", "1", "2", "3", "4", "5", "6"]
    assert shape["chain_length"] == 7
    assert shape["parallel_width"] == 2
    assert shape["engineer_width"] == 2
    assert shape["warning"] == "engineer width 2 is below engineer cap 4"


@pytest.mark.parametrize(
    ("tasks", "message"),
    [
        ([{"id": "a", "depends_on": ["missing"]}], "unknown dependencies: missing"),
        ([{"id": "a", "depends_on": ["b"]}, {"id": "b", "depends_on": ["a"]}], "contain a cycle"),
    ],
)
def test_invalid_dependencies_are_reported(tasks, message):
    from scripts.swarm.store import SwarmError

    with pytest.raises(SwarmError, match=message):
        analyze(tasks)


def test_plan_shape_counts_remaining_work_and_completed_dependencies_do_not_extend_chain():
    tasks = [
        {"id": "done", "state": "done", "done": True},
        {"id": "first", "state": "claimed", "depends_on": ["done"]},
        {"id": "second", "state": "open", "depends_on": ["first"]},
        {"id": "parallel", "state": "blocked"},
        {"id": "deleted", "state": "open", "out_of_scope": True},
    ]
    shape = analyze(tasks)
    assert shape["chain_length"] == 2
    assert shape["parallel_width"] == 2
    assert shape["critical_path"] == ["first", "second"]
    assert shape["has_dependencies"] is True
    assert analyze([tasks[0]])["chain_length"] == 0
    assert analyze([tasks[0]])["has_dependencies"] is False
