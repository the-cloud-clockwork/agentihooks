"""A warm point write rebuilds only the fragments and writer copy leaves its operations change."""

import copy
import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast

import orjson
import pytest

from scripts.swarm_ledger.repository import fragments, tracked, writer_copy
from scripts.swarm_ledger.repository.entry import Entry, thawed
from scripts.swarm_ledger.repository.fragments import Fragment, document, encoded

FEATURE = "ledger-write-partial-rebuild"


def ledger(tasks: int) -> dict[str, Any]:
    """A ledger of ``tasks`` tasks, each with one stamp record, two comments and a proof."""
    return {
        "_meta": {
            "rev": 1,
            "stamps": {f"tasks/t{n}/done": {"at": n, "rev": 1, "by": "swarm"} for n in range(tasks)},
            "events": [{"rev": 1, "kind": "joined"}],
            "members": {"engineer": {"last_seen": 1}},
        },
        "title": "Ledger",
        "tasks": [
            {
                "id": f"t{n}",
                "state": "open",
                "comments": [{"id": "b", "text": "second"}, {"id": "a", "text": "first"}],
                "proof": {"evidence": "kept", "filed": "no"},
            }
            for n in range(tasks)
        ],
    }


def warm(tasks: int) -> tuple[Fragment, dict[str, Any]]:
    """The stored fragments and writer copy a warm write leaves on a ledger of ``tasks`` tasks."""
    stored, writer = writer_copy.written(ledger(tasks), None)
    writer["_meta"]["rev"] = 2
    return writer_copy.written(writer, stored)


def comment(writer: dict[str, Any]) -> None:
    """Add one comment to task t1 the way a point comment write does."""
    writer["_meta"]["rev"] = 3
    writer["tasks"][1]["comments"].append({"id": "new", "text": "landed"})
    writer["_meta"]["stamps"]["tasks/t1/comments"] = {"at": 3, "rev": 3, "by": "engineer"}


def task_parts(stored: Fragment) -> tuple[Fragment, ...]:
    """The task fragments ``stored`` holds."""
    parts = cast(dict[str, Fragment], stored.parts)
    return cast(tuple[Fragment, ...], parts["tasks"].parts)


def fresh(writer: dict[str, Any]) -> str:
    """The text a full encoding of the writer copy stores."""
    return encoded(document(cast(dict[str, Any], thawed(writer))))


def serialized(monkeypatch: pytest.MonkeyPatch, tasks: int) -> int:
    """How many values one point comment write serializes on a warm ledger of ``tasks`` tasks."""
    stored, writer = warm(tasks)
    comment(writer)
    calls: list[object] = []

    def dumps(value: object) -> bytes:
        calls.append(value)
        return orjson.dumps(value)

    with monkeypatch.context() as patched:
        patched.setattr(fragments, "orjson", SimpleNamespace(dumps=dumps, JSONEncodeError=orjson.JSONEncodeError))
        writer_copy.written(writer, stored)
    return len(calls)


@pytest.mark.feature("ledger-write-partial-rebuild")
def test_a_point_write_serializes_as_few_values_on_four_thousand_untouched_tasks_as_on_two_thousand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    small, large = serialized(monkeypatch, 2000), serialized(monkeypatch, 4000)
    assert small == large
    assert large <= 4


@pytest.mark.feature("ledger-write-partial-rebuild")
def test_untouched_tasks_keep_their_stored_fragments_and_writer_objects() -> None:
    stored, writer = warm(50)
    held, parts = list(writer["tasks"]), task_parts(stored)
    comment(writer)
    after, rewritten = writer_copy.written(writer, stored)
    rebuilt = task_parts(after)
    assert [rebuilt[n] is parts[n] for n in range(50)] == [n != 1 for n in range(50)]
    assert [rewritten["tasks"][n] is held[n] for n in range(50)] == [n != 1 for n in range(50)]
    assert cast(dict[str, Any], rebuilt[1].value)["comments"][-1] == {"id": "new", "text": "landed"}
    assert encoded(after) == fresh(rewritten)
    assert tracked.clean(rewritten["tasks"][1]) is rebuilt[1]
    assert rewritten["tasks"][1] is not rebuilt[1].value


WRITES: dict[str, Callable[[dict[str, Any]], object]] = {
    "dict item": lambda task: task["proof"].__setitem__("evidence", "changed"),
    "dict delete": lambda task: task["proof"].__delitem__("filed"),
    "dict in place or": lambda task: task["proof"].__ior__({"extra": 1}),
    "dict clear": lambda task: task["proof"].clear(),
    "dict popitem": lambda task: task["proof"].popitem(),
    "dict update": lambda task: task["proof"].update(extra=1),
    "dict setdefault of a new key": lambda task: task["proof"].setdefault("extra", 1),
    "dict pop of a present key": lambda task: task["proof"].pop("filed"),
    "leaf item": lambda task: task.__setitem__("state", "done"),
    "list item": lambda task: task["comments"].__setitem__(0, {"id": "c"}),
    "list delete": lambda task: task["comments"].__delitem__(0),
    "list in place add": lambda task: task["comments"].__iadd__([{"id": "c"}]),
    "list in place multiply": lambda task: task["comments"].__imul__(2),
    "list append": lambda task: task["comments"].append({"id": "c"}),
    "list extend": lambda task: task["comments"].extend([{"id": "c"}]),
    "list insert": lambda task: task["comments"].insert(0, {"id": "c"}),
    "list pop": lambda task: task["comments"].pop(),
    "list remove": lambda task: task["comments"].remove(task["comments"][0]),
    "list clear": lambda task: task["comments"].clear(),
    "list sort": lambda task: task["comments"].sort(key=lambda item: item["id"]),
    "list reverse": lambda task: task["comments"].reverse(),
    "nested in a list": lambda task: task["comments"][0].__setitem__("text", "changed"),
}


@pytest.mark.feature("ledger-write-partial-rebuild")
@pytest.mark.parametrize("write", list(WRITES), ids=list(WRITES))
def test_every_write_inside_an_untouched_task_reaches_the_stored_fragments(write: str) -> None:
    stored, writer = warm(3)
    parts = task_parts(stored)
    WRITES[write](writer["tasks"][2])
    assert tracked.clean(writer["tasks"][2]) is None
    after, rewritten = writer_copy.written(writer, stored)
    assert task_parts(after)[2] is not parts[2]
    assert task_parts(after)[2].value == thawed(writer["tasks"][2]) == rewritten["tasks"][2]
    assert encoded(after) == fresh(rewritten)


READS: dict[str, Callable[[dict[str, Any]], object]] = {
    "dict setdefault of a present key": lambda task: task["proof"].setdefault("evidence", "other"),
    "dict pop of an absent key": lambda task: task["proof"].pop("absent", None),
    "dict get": lambda task: task["proof"].get("evidence"),
    "assigning the held object again": lambda task: task.__setitem__("state", task["state"]),
    "list index": lambda task: task["comments"][0]["text"],
    "list copy": lambda task: list(task["comments"]),
}


@pytest.mark.feature("ledger-write-partial-rebuild")
@pytest.mark.parametrize("read", list(READS), ids=list(READS))
def test_reads_leave_an_untouched_task_on_its_stored_fragment(read: str) -> None:
    stored, writer = warm(3)
    READS[read](writer["tasks"][2])
    assert tracked.clean(writer["tasks"][2]) is task_parts(stored)[2]


@pytest.mark.feature("ledger-write-partial-rebuild")
def test_a_container_moved_out_of_its_task_never_claims_that_task_fragment() -> None:
    stored, writer = warm(3)
    proof = writer["tasks"][0]["proof"]
    writer["tasks"].append(proof)
    assert tracked.clean(proof) is None
    assert tracked.clean(writer["tasks"][0]) is task_parts(stored)[0]
    after, rewritten = writer_copy.written(writer, stored)
    assert task_parts(after)[3].value == {"evidence": "kept", "filed": "no"}
    assert encoded(after) == fresh(rewritten)


@pytest.mark.feature("ledger-write-partial-rebuild")
def test_a_task_moved_to_another_position_keeps_its_stored_fragment() -> None:
    stored, writer = warm(4)
    parts = task_parts(stored)
    writer["tasks"].insert(0, writer["tasks"].pop())
    after, rewritten = writer_copy.written(writer, stored)
    assert [part is old for part, old in zip(task_parts(after), (parts[3], *parts[:3]))] == [True] * 4
    assert encoded(after) == fresh(rewritten)


@pytest.mark.feature("ledger-write-partial-rebuild")
def test_copies_of_a_writer_task_are_plain_and_a_change_through_a_shared_container_reaches_the_task() -> None:
    stored, writer = warm(2)
    task = writer["tasks"][0]
    deep, shallow = copy.deepcopy(task), copy.copy(task)
    assert type(deep) is dict and type(deep["comments"]) is list and type(deep["comments"][0]) is dict
    assert type(shallow) is dict and deep == shallow == task
    assert tracked.clean(task) is task_parts(stored)[0]
    shallow["comments"].append({"id": "shared"})
    assert tracked.clean(task) is None
    assert type(copy.copy(task["comments"])) is list


@pytest.mark.feature("ledger-write-partial-rebuild")
def test_a_writer_copy_built_from_stored_fragments_tracks_every_leaf_and_shares_stamp_records() -> None:
    stored, _ = warm(3)
    built = Entry(5, None, cast(dict[str, Any], stored.value), stored).for_write()
    assert built == stored.value
    assert list(built) == list(cast(dict[str, Any], stored.value))
    assert [tracked.clean(task) for task in built["tasks"]] == list(task_parts(stored))
    assert built["tasks"][0] is not task_parts(stored)[0].value
    records = cast(dict[str, Any], stored.value)["_meta"]["stamps"]
    assert all(built["_meta"]["stamps"][key] is record for key, record in records.items())
    assert built["_meta"]["stamps"] is not records
    meta = cast(dict[str, Fragment], cast(dict[str, Fragment], stored.parts)["_meta"].parts)
    assert tracked.clean(built["_meta"]["members"]) is meta["members"]
    built["tasks"][0]["state"] = "done"
    assert cast(dict[str, Any], stored.value)["tasks"][0]["state"] == "open"


@pytest.mark.feature("ledger-write-partial-rebuild")
def test_an_entry_without_fragments_still_hands_out_a_plain_copy() -> None:
    state = ledger(2)
    built = Entry(5, None, state).for_write()
    assert built == state
    assert type(built["tasks"][0]) is dict
    assert tracked.clean(built["tasks"][0]) is None


@pytest.mark.feature("ledger-write-partial-rebuild")
def test_a_plain_task_in_a_changed_list_is_tracked_again_after_the_write() -> None:
    stored, writer = warm(3)
    writer["tasks"][2] = json.loads(json.dumps(writer["tasks"][2]))
    comment(writer)
    after, rewritten = writer_copy.written(writer, stored)
    assert task_parts(after)[2] is task_parts(stored)[2]
    assert tracked.clean(rewritten["tasks"][2]) is task_parts(after)[2]
    assert rewritten["tasks"][2] is not task_parts(after)[2].value
    assert encoded(after) == fresh(rewritten)


@pytest.mark.feature("ledger-write-partial-rebuild")
def test_a_task_whose_content_came_back_keeps_its_fragment_and_is_tracked_again_once_its_list_changes() -> None:
    stored, writer = warm(3)
    writer["tasks"][2]["state"] = "done"
    writer["tasks"][2]["state"] = "open"
    after, rewritten = writer_copy.written(writer, stored)
    assert after is stored
    assert tracked.clean(rewritten["tasks"][2]) is None
    comment(rewritten)
    final, healed = writer_copy.written(rewritten, after)
    assert task_parts(final)[2] is task_parts(stored)[2]
    assert tracked.clean(healed["tasks"][2]) is task_parts(final)[2]
    assert encoded(final) == fresh(healed)
