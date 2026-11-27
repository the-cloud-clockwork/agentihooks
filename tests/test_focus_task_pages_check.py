"""The live check of the paged focus read over a stand-in store and reader."""

import urllib.error
from email.message import Message
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlencode

import pytest

from scripts.feature_checks import focus_task_pages as check
from scripts.swarm_ledger.api import resources

pytestmark = pytest.mark.unit


def row(task_id: str, verdict: str = "claimable") -> dict[str, Any]:
    return {"id": task_id, "lane": "eng", "state": "open", "verdict": verdict}


def revised(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**found, "revision": f"rev-{found['id']}"} for found in rows]


class Ledgers:
    """Answers the reads of the check from the task rows of each focus target, and keeps what was read. The
    pages of a target come from its own script when it has one: one answer per read, the last one repeated. A
    path named in refused is answered with that HTTP status, and the freezes read lists the frozen targets ahead
    of the focus records. The cursor question of a target one reply cannot hold is answered as the ledger answers
    it: a conflict when the cursor's revision is not the revision of the target's current rows, moved by its own
    script when given, else held to the target's own rows."""

    def __init__(
        self,
        targets: dict[str, list[dict[str, Any]]],
        pages: dict[str, list[Any]] | None = None,
        refused: dict[str, int] | None = None,
        frozen: tuple[str, ...] = (),
        moved: dict[str, list[list[dict[str, Any]]]] | None = None,
        probe_refused: dict[str, int] | None = None,
    ) -> None:
        self.targets, self.pages, self.reads = targets, pages or {}, []
        self.refused, self.frozen = refused or {}, frozen
        self.moved, self.probe_refused = moved or {}, probe_refused or {}

    def __call__(self, slug: str, path: str, collection: bool) -> Any:
        self.reads.append((slug, path, collection))
        base, _, query = path.partition("?")
        if base in self.refused:
            raise urllib.error.HTTPError(path, self.refused[base], "refused", Message(), None)
        if base == "freezes":
            return [
                *({"target": name, "verb": "freeze"} for name in self.frozen),
                *({"target": name, "verb": "focus"} for name in self.targets),
            ]
        if base == "focus":
            return [{"target": name, "tasks": rows, "by": "operator"} for name, rows in self.targets.items()]
        name = base.removeprefix("focus/")
        if not name.endswith("/tasks"):
            return {"target": name, "claimable": 0, "done": [], "tasks": self.targets[name]}
        name = name.removesuffix("/tasks")
        if query:
            return self._probe(name, query, path)
        scripted = self.pages.get(name)
        if not scripted:
            return revised(self._truth(name))
        return scripted.pop(0) if len(scripted) > 1 else scripted[0]

    def _truth(self, name: str) -> list[dict[str, Any]]:
        moving = self.moved.get(name)
        if not moving:
            return self.targets[name]
        return moving.pop(0) if len(moving) > 1 else moving[0]

    def _probe(self, name: str, query: str, path: str) -> dict[str, Any]:
        if name in self.probe_refused:
            raise urllib.error.HTTPError(path, self.probe_refused[name], "refused", Message(), None)
        cursor = parse_qs(query)["cursor"][0]
        if cursor != f"{resources.revision(self._truth(name))}:0":
            raise urllib.error.HTTPError(path, 409, "conflict", Message(), None)
        return {"data": [], "revision": cursor, "next_cursor": None}


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
        ("rig", "focus/phases/p1/tasks", True),
        ("rig", "focus/phases/p1", False),
    ]


@pytest.mark.feature("ledger-focus-task-pages")
def test_a_ledger_that_moves_while_the_pages_are_read_passes_on_the_first_reading() -> None:
    moved = [row("t1", "it is in a pull request"), row("t2")]
    held: dict[str, list[dict[str, Any]]] = {"lane:eng": [row("t1"), row("t2")]}
    reads: list[str] = []

    def read(slug: str, path: str, listed: bool) -> Any:
        reads.append(path)
        if path == "focus":
            return [{"target": "lane:eng", "tasks": held["lane:eng"]}]
        if listed:
            held["lane:eng"] = moved
            return revised(moved)
        return {"target": "lane:eng", "tasks": held["lane:eng"]}

    assert check.lines("rig", read) == ["rig focus lane:eng: 2 task rows in pages, as the read of the target holds"]
    assert reads == ["focus", "focus/lane:eng/tasks", "focus/lane:eng"]


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
        *[("rig", "focus/tasks/t1/tasks", True), ("rig", "focus/tasks/t1", False)] * 3,
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
    rows = [row("t1"), row("t2")]
    ledgers = Ledgers({"phases/p1": rows}, refused={"focus/phases/p1": 413})
    assert check.lines("rig", ledgers) == ["rig focus phases/p1: 2 task rows in pages, more than one reply holds"]
    probe = "focus/phases/p1/tasks?" + urlencode({"limit": 1, "cursor": f"{resources.revision(rows)}:0"})
    assert ledgers.reads == [
        ("rig", "focus", True),
        ("rig", "focus/phases/p1/tasks", True),
        ("rig", "focus/phases/p1", False),
        ("rig", probe, False),
    ]


@pytest.mark.feature("ledger-focus-task-pages")
def test_a_target_one_reply_cannot_hold_whose_pages_miss_a_row_fails_after_three_reads() -> None:
    ledgers = Ledgers(
        {"phases/p1": [row("t1"), row("t2")]},
        pages={"phases/p1": [revised([row("t1")])]},
        refused={"focus/phases/p1": 413},
    )
    assert check.lines("rig", ledgers) == [
        "FAIL: rig focus phases/p1: the pages hold 1 task rows that are not the list the ledger holds"
    ]
    assert len(ledgers.reads) == 1 + 3 * 3


@pytest.mark.feature("ledger-focus-task-pages")
def test_a_target_one_reply_cannot_hold_whose_pages_repeat_a_row_fails_after_three_reads() -> None:
    ledgers = Ledgers(
        {"phases/p1": [row("t1"), row("t2")]},
        pages={"phases/p1": [revised([row("t1"), row("t1")])]},
        refused={"focus/phases/p1": 413},
    )
    assert check.lines("rig", ledgers) == [
        "FAIL: rig focus phases/p1: the pages hold 2 task rows that are not the list the ledger holds"
    ]


@pytest.mark.feature("ledger-focus-task-pages")
def test_a_target_one_reply_cannot_hold_whose_ledger_moved_once_then_holds_passes_on_the_second_reading() -> None:
    ledgers = Ledgers(
        {"phases/p1": [row("t1"), row("t2")]},
        refused={"focus/phases/p1": 413},
        moved={"phases/p1": [[row("t1"), row("t2")], [row("t1", "it waits on task t9"), row("t2")]]},
    )
    assert check.lines("rig", ledgers) == ["rig focus phases/p1: 2 task rows in pages, more than one reply holds"]
    assert len(ledgers.reads) == 1 + 2 * 3


@pytest.mark.feature("ledger-focus-task-pages")
def test_a_refusal_other_than_the_conflict_on_the_cursor_question_is_raised() -> None:
    ledgers = Ledgers(
        {"phases/p1": [row("t1"), row("t2")]},
        refused={"focus/phases/p1": 413},
        probe_refused={"phases/p1": 500},
    )
    with pytest.raises(urllib.error.HTTPError) as raised:
        check.lines("rig", ledgers)
    assert raised.value.code == 500


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


@pytest.mark.feature("ledger-focus-task-pages")
def test_main_with_no_argument_builds_its_store_and_read_from_the_ledger(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.swarm import store as swarm_store
    from scripts.swarm_ledger import ledger as swarm_ledger

    built: list[Any] = []

    class Client:
        pass

    def redis_client() -> Client:
        client = Client()
        built.append(client)
        return client

    class Stub:
        def __init__(self, client: Any) -> None:
            self.client = client

        def slugs(self) -> list[str]:
            return ["rig"]

        def config(self, slug: str) -> SimpleNamespace:
            return SimpleNamespace(state="running")

    reads: list[tuple[str, str, bool]] = []

    def resource(slug: str, path: str, collection: bool = False) -> Any:
        reads.append((slug, path, collection))
        if path == "focus":
            return [{"target": "phases/p1", "tasks": [row("t1")]}]
        if path == "focus/phases/p1":
            return {"target": "phases/p1", "tasks": [row("t1")]}
        return revised([row("t1")])

    monkeypatch.setattr(swarm_store, "RedisStore", Stub)
    monkeypatch.setattr(swarm_store, "redis_client", redis_client)
    monkeypatch.setattr(swarm_ledger, "resource", resource)
    assert check.main() == 0
    assert len(built) == 1
    assert reads[0] == ("rig", "focus", True)
    assert capsys.readouterr().out.splitlines() == [
        "rig focus phases/p1: 1 task rows in pages, as the read of the target holds"
    ]
