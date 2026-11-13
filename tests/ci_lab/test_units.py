from dataclasses import FrozenInstanceError

import pytest

from scripts.ci_lab import units


def test_unit_and_grade_are_frozen() -> None:
    unit = units.Unit(kind="test", key="key", members=("tests/a.py::test_a",))
    grade = units.Grade(
        repo="owner/repo", head="head", base="base", units=(unit,), priority=units.Priority.PULL_REQUEST
    )
    assert (unit.kind, unit.key, unit.members) == ("test", "key", ("tests/a.py::test_a",))
    assert (grade.repo, grade.head, grade.base, grade.units, grade.priority) == (
        "owner/repo",
        "head",
        "base",
        (unit,),
        units.Priority.PULL_REQUEST,
    )
    with pytest.raises(FrozenInstanceError):
        setattr(unit, "key", "changed")
    with pytest.raises(FrozenInstanceError):
        setattr(grade, "head", "changed")
    assert hash(unit)
    assert hash(grade)


@pytest.mark.parametrize(
    ("event", "priority"),
    [
        ({"event_name": "pull_request", "hotfix": True}, "HOTFIX"),
        ({"event_name": "pull_request"}, "PULL_REQUEST"),
        ({"event_name": "merge_group"}, "QUEUE"),
        ({"event_name": "push"}, "PROMOTION"),
        ({"event_name": "backfill"}, "BACKFILL"),
    ],
)
def test_event_priority(event: dict[str, object], priority: str) -> None:
    assert units.priority_of(event) is units.Priority[priority]


def test_priority_order_is_total() -> None:
    ordered = list(units.Priority)
    assert [int(item) for item in ordered] == list(range(5))
    assert [item.name for item in ordered] == ["HOTFIX", "PULL_REQUEST", "QUEUE", "PROMOTION", "BACKFILL"]
    for left_index, left in enumerate(ordered):
        for right_index, right in enumerate(ordered):
            assert (left < right) == (left_index < right_index)
            assert (left == right) == (left_index == right_index)


def test_unknown_event_is_rejected() -> None:
    with pytest.raises(ValueError) as error:
        units.priority_of({"event_name": "unknown"})
    assert str(error.value) == "Unknown lab event: unknown"
