"""A warm point write serializes only the values it changed, however many untouched tasks the ledger holds, and every
untouched task keeps its stored fragment and its writer copy object."""

from typing import Any, cast

import pytest

from scripts.swarm_ledger.repository import fragments, writer_copy
from scripts.swarm_ledger.repository.entry import Entry
from scripts.swarm_ledger.repository.fragments import Fragment, document, encoded


def child(fragment: Fragment, *keys: str | int) -> Fragment:
    """The fragment that ``keys`` name under ``fragment``, a key for a mapping and an index for a list."""
    for key in keys:
        parts = cast(Any, fragment.parts)
        assert parts is not None
        fragment = parts[key]
    return fragment


def ledger(untouched: int) -> dict[str, Any]:
    """A ledger with one task to claim and ``untouched`` tasks no write changes."""
    tasks = [
        {"id": f"t{n}", "state": "open", "comments": [{"text": f"note {n}"}], "territory": ["scripts"]}
        for n in range(untouched + 1)
    ]
    meta = {"rev": 1, "stamps": {}, "events": [{"rev": 1, "kind": "seed"}], "crew": {"lead": ["eng"]}}
    return {"_meta": meta, "tasks": tasks, "chat": []}


def claim(writer: dict[str, Any]) -> None:
    """The point write: claim the first task and move the revision."""
    writer["_meta"]["rev"] = 2
    writer["tasks"][0]["state"] = "claimed"


def serialized(patch: pytest.MonkeyPatch, writer: dict[str, Any], previous: Fragment) -> tuple[int, Fragment, dict]:
    """The values one writer copy rebuild serializes, with the fragments and writer copy it returns."""
    dumped: list[object] = []
    dumps = fragments.orjson.dumps
    with patch.context() as counting:
        counting.setattr(fragments.orjson, "dumps", lambda value: dumped.append(value) or dumps(value))
        rebuilt, held = writer_copy.written(writer, previous)
    return len(dumped), rebuilt, held


def warm(untouched: int, patch: pytest.MonkeyPatch) -> tuple[int, Fragment, Fragment, dict, dict]:
    """A point write through the writer copy a first write left on a ledger of ``untouched`` other tasks."""
    published, writer = writer_copy.written(ledger(untouched), None)
    claim(writer)
    count, rebuilt, held = serialized(patch, writer, published)
    return count, published, rebuilt, writer, held


def cold(untouched: int, patch: pytest.MonkeyPatch) -> int:
    """The values a point write serializes through the writer copy a stored entry builds from its fragments."""
    published = document(ledger(untouched))
    writer = Entry(1, None, published.value, published).for_write()
    claim(writer)
    return serialized(patch, writer, published)[0]


@pytest.mark.feature("ledger-write-partial-rebuild")
def test_a_warm_point_write_serializes_as_many_values_on_four_thousand_untouched_tasks_as_on_two_thousand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert warm(4000, monkeypatch)[0] == warm(2000, monkeypatch)[0]


@pytest.mark.feature("ledger-write-partial-rebuild")
def test_a_point_write_from_a_stored_entry_serializes_as_many_values_on_four_thousand_tasks_as_on_two_thousand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert cold(4000, monkeypatch) == cold(2000, monkeypatch)


@pytest.mark.feature("ledger-write-partial-rebuild")
def test_untouched_tasks_keep_their_stored_fragment_and_writer_object(monkeypatch: pytest.MonkeyPatch) -> None:
    _, published, rebuilt, writer, held = warm(50, monkeypatch)
    assert all(child(rebuilt, "tasks", n) is child(published, "tasks", n) for n in range(1, 51))
    assert all(held["tasks"][n] is writer["tasks"][n] for n in range(1, 51))
    assert child(rebuilt, "tasks", 0) is not child(published, "tasks", 0)
    assert child(rebuilt, "_meta", "crew") is child(published, "_meta", "crew")
    assert held["_meta"]["crew"] is writer["_meta"]["crew"]


@pytest.mark.feature("ledger-write-partial-rebuild")
def test_a_write_that_removes_the_first_task_keeps_every_later_task_fragment_and_writer_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published, writer = writer_copy.written(ledger(20), None)
    later = writer["tasks"][1:]
    writer["tasks"].pop(0)
    count, rebuilt, held = serialized(monkeypatch, writer, published)
    assert count == 0
    assert all(child(rebuilt, "tasks", n) is child(published, "tasks", n + 1) for n in range(20))
    assert all(task is kept for task, kept in zip(held["tasks"], later, strict=True))
    expected = ledger(20)
    del expected["tasks"][0]
    assert rebuilt.value == held == expected
    assert encoded(rebuilt) == encoded(document(expected))


@pytest.mark.feature("ledger-write-partial-rebuild")
def test_a_write_copy_built_from_stored_fragments_shares_only_stamp_records_with_the_stored_state() -> None:
    state = ledger(3)
    state["_meta"]["stamps"] = {"tasks/t1/state": {"at": 1, "rev": 1, "by": "eng"}}
    published = document(state)
    stored = cast(dict[str, Any], published.value)
    writer = Entry(1, None, stored, published).for_write()
    assert writer == stored
    assert writer["_meta"]["stamps"] is not stored["_meta"]["stamps"]
    assert writer["_meta"]["stamps"]["tasks/t1/state"] is stored["_meta"]["stamps"]["tasks/t1/state"]
    assert writer["tasks"] is not stored["tasks"]
    assert all(task is not kept for task, kept in zip(writer["tasks"], stored["tasks"], strict=True))
    assert writer["_meta"]["crew"] is not stored["_meta"]["crew"]
    assert writer["tasks"][1]["comments"] is not stored["tasks"][1]["comments"]
    writer["tasks"][1]["comments"].append({"text": "writer only"})
    assert stored["tasks"][1]["comments"] == [{"text": "note 1"}]


@pytest.mark.feature("ledger-write-partial-rebuild")
def test_a_warm_point_write_stores_what_a_fresh_encoding_of_the_written_state_stores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, rebuilt, writer, held = warm(5, monkeypatch)
    expected = ledger(5)
    claim(expected)
    assert rebuilt.value == held == writer == expected
    assert encoded(rebuilt) == encoded(document(expected))
    assert held["tasks"][0] is not cast(dict[str, Any], rebuilt.value)["tasks"][0]
    held["tasks"][1]["comments"].append({"text": "next write"})
    again, kept = writer_copy.written(held, rebuilt)
    expected["tasks"][1]["comments"].append({"text": "next write"})
    assert again.value == kept == expected
    assert child(again, "tasks", 1) is not child(rebuilt, "tasks", 1)
    assert child(again, "tasks", 2) is child(rebuilt, "tasks", 2)
