"""The live check of the paged focus read over a stand-in store and reader."""

import urllib.error
from email.message import Message
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.feature_checks import focus_task_pages as check

pytestmark = pytest.mark.unit


def row(task_id: str, verdict: str = "claimable") -> dict[str, Any]:
    return {"id": task_id, "lane": "eng", "state": "open", "verdict": verdict}


def revised(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**found, "revision": f"rev-{found['id']}"} for found in rows]


class Ledgers:
    """Answers the reads of the check from the task rows of each focus target, and keeps what was read. The
    pages of a target come from its own script when it has one: one answer per read, the last one repeated. A
    path named in refused is answered with that HTTP status, and the freezes read lists the frozen targets ahead
    of the focus records."""

    def __init__(
        self,
        targets: dict[str, list[dict[str, Any]]],
        pages: dict[str, list[Any]] | None = None,
        refused: dict[str, int] | None = None,
        frozen: tuple[str, ...] = (),
    ) -> None:
        self.targets, self.pages, self.reads = targets, pages or {}, []
        self.refused, self.frozen = refused or {}, frozen

    def __call__(self, slug: str, path: str, collection: bool) -> Any:
        self.reads.append((slug, path, collection))
        if path in self.refused:
            raise urllib.error.HTTPError(path, self.refused[path], "refused", Message(), None)
        if path == "freezes":
            return [
                *({"target": name, "verb": "freeze"} for name in self.frozen),
                *({"target": name, "verb": "focus"} for name in self.targets),
            ]
        if path == "focus":
            return [{"target": name, "tasks": rows, "by": "operator"} for name, rows in self.targets.items()]
        name = path.removeprefix("focus/")
        if not name.endswith("/tasks"):
            return {"target": name, "claimable": 0, "done": [], "tasks": self.targets[name]}
        name = name.removesuffix("/tasks")
        scripted = self.pages.get(name)
        if not scripted:
            return revised(self.targets[name])
        return scripted.pop(0) if len(scripted) > 1 else scripted[0]


class Store:
    def __init__(self, states: dict[str, str]) -> None:
        self.states = states

    def slugs(self) -> list[str]:
        return list(self.states)

    def config(self, slug: str) -> SimpleNamespace:
        return SimpleNamespace(state=self.states[slug])


@pytest.mark.feature("ledger-focus-task-pages")
def test_a_target_whose_pages_hold_the_rows_of_its_one_reply_read_passes() -> None:
    ledgers = Ledgers({"phases/p1": [row("t1"), row("t2", "it waits on task t9")]})
    assert check.lines("rig", ledgers) == ["rig focus phases/p1: 2 task rows in pages, as the read of the target holds"]
    assert ledgers.reads == [
        ("rig", "focus", True),
        ("rig", "focus/phases/p1", False),
        ("rig", "focus/phases/p1/tasks", True),
    ]


@pytest.mark.feature("ledger-focus-task-pages")
@pytest.mark.parametrize(
    ("paged", "count"),
    [
        ([row("t1")], 1),
        ([row("t1"), row("t2", "it is out of scope")], 2),
        ([row("t2"), row("t1")], 2),
        ([row("t1"), row("t2"), row("t3")], 3),
        ([], 0),
    ],
)
def test_pages_that_keep_differing_from_the_one_reply_read_fail_after_three_reads(
    paged: list[dict[str, Any]], count: int
) -> None:
    ledgers = Ledgers({"tasks/t1": [row("t1"), row("t2")]}, {"tasks/t1": [revised(paged)]})
    assert check.lines("rig", ledgers) == [
        f"FAIL: rig focus tasks/t1: the pages hold {count} task rows that differ from the 2 the target holds"
    ]
    assert ledgers.reads == [
        ("rig", "focus", True),
        *[("rig", "focus/tasks/t1", False), ("rig", "focus/tasks/t1/tasks", True)] * 3,
    ]


@pytest.mark.feature("ledger-focus-task-pages")
def test_a_ledger_that_moved_between_the_two_reads_is_read_again_and_passes() -> None:
    rows = [row("t1"), row("t2")]
    ledgers = Ledgers({"lane:eng": rows}, {"lane:eng": [revised([row("t1")]), revised(rows)]})
    assert check.lines("rig", ledgers) == ["rig focus lane:eng: 2 task rows in pages, as the read of the target holds"]
    assert len(ledgers.reads) == 5


@pytest.mark.feature("ledger-focus-task-pages")
def test_the_three_focus_records_with_the_most_tasks_are_checked_largest_first() -> None:
    sizes = {"phases/one": 1, "phases/four": 4, "phases/two": 2, "phases/three": 3, "phases/none": 0}
    ledgers = Ledgers({name: [row(f"t{number}") for number in range(count)] for name, count in sizes.items()})
    assert check.lines("rig", ledgers) == [
        "rig focus phases/four: 4 task rows in pages, as the read of the target holds",
        "rig focus phases/three: 3 task rows in pages, as the read of the target holds",
        "rig focus phases/two: 2 task rows in pages, as the read of the target holds",
    ]


@pytest.mark.feature("ledger-focus-task-pages")
def test_a_ledger_with_no_focus_record_says_so() -> None:
    assert check.lines("rig", Ledgers({})) == ["rig holds no focus record"]


@pytest.mark.feature("ledger-focus-task-pages")
def test_a_target_one_reply_cannot_hold_passes_on_its_pages() -> None:
    ledgers = Ledgers({"phases/p1": [row("t1"), row("t2")]}, refused={"focus/phases/p1": 413})
    assert check.lines("rig", ledgers) == ["rig focus phases/p1: 2 task rows in pages, more than one reply holds"]
    assert ledgers.reads == [
        ("rig", "focus", True),
        ("rig", "focus/phases/p1", False),
        ("rig", "focus/phases/p1/tasks", True),
    ]


@pytest.mark.feature("ledger-focus-task-pages")
def test_a_focus_list_one_reply_cannot_hold_is_listed_through_the_freezes_read() -> None:
    targets = {f"phases/p{number}": [row(f"t{number}")] for number in range(1, 5)}
    ledgers = Ledgers(targets, refused={"focus": 413}, frozen=("tasks/t9",))
    assert check.lines("rig", ledgers) == [
        "rig focus phases/p1: 1 task rows in pages, as the read of the target holds",
        "rig focus phases/p2: 1 task rows in pages, as the read of the target holds",
        "rig focus phases/p3: 1 task rows in pages, as the read of the target holds",
    ]
    assert ledgers.reads[:2] == [("rig", "focus", True), ("rig", "freezes", True)]
    assert ("rig", "focus/tasks/t9", False) not in ledgers.reads


@pytest.mark.feature("ledger-focus-task-pages")
@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("focus", 404),
        ("focus", 500),
        ("focus/phases/p1", 404),
        ("focus/phases/p1", 500),
        ("focus/phases/p1/tasks", 404),
        ("focus/phases/p1/tasks", 413),
    ],
)
def test_a_read_refused_for_another_reason_is_raised(path: str, status: int) -> None:
    ledgers = Ledgers({"phases/p1": [row("t1")]}, refused={path: status})
    with pytest.raises(urllib.error.HTTPError) as raised:
        check.lines("rig", ledgers)
    assert raised.value.code == status


@pytest.mark.feature("ledger-focus-task-pages")
def test_the_check_reads_every_swarm_that_is_not_stopped_in_name_order_and_passes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledgers = Ledgers({"phases/p1": [row("t1")]})
    assert check.main(Store({"rig-b": "running", "old": "stopped", "rig-a": "paused"}), ledgers) == 0
    assert capsys.readouterr().out.splitlines() == [
        "rig-a focus phases/p1: 1 task rows in pages, as the read of the target holds",
        "rig-b focus phases/p1: 1 task rows in pages, as the read of the target holds",
    ]
    assert {slug for slug, _, _ in ledgers.reads} == {"rig-a", "rig-b"}


@pytest.mark.feature("ledger-focus-task-pages")
def test_the_check_fails_when_one_target_fails_and_still_prints_every_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    targets = {"phases/p1": [row("t1"), row("t2")], "phases/p2": [row("t3")]}
    ledgers = Ledgers(targets, {"phases/p2": [revised([row("t3", "it is frozen")])]})
    assert check.main(Store({"rig": "running"}), ledgers) == 1
    assert capsys.readouterr().out.splitlines() == [
        "rig focus phases/p1: 2 task rows in pages, as the read of the target holds",
        "FAIL: rig focus phases/p2: the pages hold 1 task rows that differ from the 1 the target holds",
    ]


def test_a_fleet_with_no_running_swarm_prints_nothing_and_passes(capsys: pytest.CaptureFixture[str]) -> None:
    assert check.main(Store({"old": "stopped"}), Ledgers({})) == 0
    assert capsys.readouterr().out == ""
