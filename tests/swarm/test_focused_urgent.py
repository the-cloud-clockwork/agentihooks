from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm import focused_urgent

pytestmark = pytest.mark.unit

URL = "https://github.com/o/r/pull/7"
URGENT = {"id": "t1", "rank": "urgent", "state": "pr", "pr_url": URL}
LATER = {"id": "t3", "rank": "urgent", "state": "open", "pr_url": URL}
OTHER = {"id": "t2", "rank": "normal", "state": "open"}


def state(rows: list[dict[str, Any]], focus: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "tasks": rows,
        "phases": [],
        "freezes": [{"verb": "focus", "target": f"tasks/{target}"} for target in focus],
    }


def ledger(rows: list[dict[str, Any]], focus: tuple[str, ...] = (), reads: list[str] | None = None) -> Any:
    reads = [] if reads is None else reads
    doc = state(rows, focus)

    def own(slug: str) -> None:
        if slug != "sw":
            pytest.fail(f"the focused urgent lookup read swarm {slug}")

    def targeting(slug: str, url: str) -> list[dict[str, Any]]:
        own(slug)
        return [row for row in rows if row.get("pr_url") == url]

    def whole(slug: str) -> dict[str, Any]:
        own(slug)
        reads.append("state")
        return doc

    return SimpleNamespace(tasks_targeting=targeting, state=whole)


@pytest.mark.feature("focused-urgent-master-queue")
def test_an_open_urgent_task_inside_the_focus_is_the_task_of_its_pull_request() -> None:
    assert focused_urgent.task("sw", URL, ledger([URGENT, OTHER], focus=("t1",))) == "t1"


@pytest.mark.feature("focused-urgent-master-queue")
def test_the_first_urgent_task_inside_the_focus_wins_over_an_earlier_one_outside_it() -> None:
    assert focused_urgent.task("sw", URL, ledger([LATER, URGENT], focus=("t1",))) == "t1"


@pytest.mark.feature("focused-urgent-master-queue")
@pytest.mark.parametrize(
    "rows, focus",
    [
        ([{**URGENT, "rank": "normal"}], ()),
        ([{**URGENT, "state": "done"}], ()),
        ([URGENT, OTHER], ("t2",)),
        ([OTHER], ()),
        ([], ()),
    ],
    ids=["normal rank", "done", "outside the focus", "no targeting task", "no task"],
)
def test_a_pull_request_without_an_open_focused_urgent_task_has_no_task(
    rows: list[dict[str, Any]], focus: tuple[str, ...]
) -> None:
    assert focused_urgent.task("sw", URL, ledger(rows, focus=focus)) is None


@pytest.mark.feature("focused-urgent-master-queue")
@pytest.mark.parametrize(
    "rows, focus",
    [
        ([URGENT, OTHER], ()),
        ([URGENT, {**OTHER, "state": "done"}], ("t2",)),
    ],
    ids=["no focus", "focus with no open work"],
)
def test_with_no_focus_open_any_urgent_task_is_the_task_of_its_pull_request(
    rows: list[dict[str, Any]], focus: tuple[str, ...]
) -> None:
    assert focused_urgent.task("sw", URL, ledger(rows, focus=focus)) == "t1"


@pytest.mark.feature("focused-urgent-master-queue")
def test_the_ledger_state_is_read_only_when_an_urgent_task_names_the_pull_request() -> None:
    reads: list[str] = []
    assert focused_urgent.task("sw", URL, ledger([{**URGENT, "rank": "normal"}], reads=reads)) is None
    assert reads == []
    assert focused_urgent.task("sw", URL, ledger([URGENT], reads=reads)) == "t1"
    assert reads == ["state"]


@pytest.mark.feature("focused-urgent-master-queue")
@pytest.mark.parametrize(
    "focus, task_id, inside",
    [
        ((), "t1", True),
        (("t1",), "t1", True),
        (("t2",), "t1", False),
        (("t2",), "t2", True),
    ],
    ids=["no focus", "focused task", "task outside the focus", "other focused task"],
)
def test_focused_admits_a_task_inside_the_open_focus_or_any_task_with_no_focus_open(
    focus: tuple[str, ...], task_id: str, inside: bool
) -> None:
    assert focused_urgent.focused(state([URGENT, OTHER], focus), task_id) is inside
