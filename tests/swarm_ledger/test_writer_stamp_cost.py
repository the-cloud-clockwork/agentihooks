"""A write's writer copy rebuild costs the stamp records it replaced, never every stamp record the ledger holds."""

import sys
from typing import Any

import pytest

from scripts.swarm_ledger.repository import fragments, writer_copy
from scripts.swarm_ledger.repository.fragments import Fragment, document, encoded


def stored(kept: int) -> tuple[Fragment, dict[str, Any]]:
    """The fragments and writer copy a first write leaves for a ledger holding ``kept`` stamp records."""
    records = {f"tasks/t{n}/done": {"at": n, "rev": 1, "by": "swarm"} for n in range(kept)}
    state = {"_meta": {"rev": 1, "stamps": records}, "tasks": [{"id": "t0", "state": "open"}, {"id": "t1"}]}
    return writer_copy.written(state, None)


def replace_one(writer: dict[str, Any]) -> None:
    """Change one task and replace its stamp record the way a task write does."""
    writer["_meta"]["rev"] = 2
    writer["tasks"][0]["state"] = "claimed"
    writer["_meta"]["stamps"]["tasks/t0/done"] = {"at": 99, "rev": 2, "by": "engineer"}


def calls_in_written(kept: int) -> int:
    """The Python calls one write's writer copy rebuild makes on a ledger holding ``kept`` stamp records."""
    published, writer = stored(kept)
    replace_one(writer)
    counted: list[int] = []

    def count(frame: object, event: str, arg: object) -> None:
        if event == "call":
            counted.append(1)

    sys.setprofile(count)
    try:
        writer_copy.written(writer, published)
    finally:
        sys.setprofile(None)
    return len(counted)


@pytest.mark.feature("ledger-write-stamp-cost")
def test_a_write_makes_as_many_calls_on_four_thousand_kept_stamp_records_as_on_two_thousand() -> None:
    assert calls_in_written(4000) == calls_in_written(2000)


@pytest.mark.feature("ledger-write-stamp-cost")
def test_the_rebuilt_writer_copy_stores_what_a_fresh_encoding_of_the_written_state_stores() -> None:
    published, writer = stored(50)
    replace_one(writer)
    expected = {
        "_meta": {"rev": 2, "stamps": dict(writer["_meta"]["stamps"])},
        "tasks": [{"id": "t0", "state": "claimed"}, {"id": "t1"}],
    }
    rebuilt, held = writer_copy.written(writer, published)
    assert rebuilt.value == held == expected
    assert encoded(rebuilt) == encoded(document(expected))
    assert held["tasks"][0] is not rebuilt.value["tasks"][0]
    assert held["tasks"][1] is not rebuilt.value["tasks"][1]
    stamps = rebuilt.parts["_meta"].parts["stamps"]
    kept = published.parts["_meta"].parts["stamps"]
    assert stamps.parts["tasks/t0/done"] is not kept.parts["tasks/t0/done"]
    assert all(stamps.parts[key] is part for key, part in kept.parts.items() if key != "tasks/t0/done")
    assert rebuilt.parts["tasks"].parts[1] is published.parts["tasks"].parts[1]


@pytest.mark.feature("ledger-write-stamp-cost")
def test_kept_stamp_records_reuse_their_fragments_without_encoding_them_again(monkeypatch: pytest.MonkeyPatch) -> None:
    previous = document({"_meta": {"stamps": {"a": {"rev": 1}, "b": {"rev": 1}, "c": {"rev": 1}}}})
    records = previous.value["_meta"]["stamps"]
    leaves: list[object] = []
    leaf = fragments.leaf
    monkeypatch.setattr(fragments, "leaf", lambda value, known=None: leaves.append(value) or leaf(value, known))
    updated = document({"_meta": {"stamps": {**records, "b": {"rev": 2}}}}, previous)
    assert [value for value in leaves if isinstance(value, dict) and "rev" in value] == [{"rev": 2}]
    assert updated.value["_meta"]["stamps"] == {"a": {"rev": 1}, "b": {"rev": 2}, "c": {"rev": 1}}


@pytest.mark.feature("ledger-write-stamp-cost")
def test_unchanged_stamp_records_keep_the_stored_stamps_fragment() -> None:
    previous = document({"_meta": {"stamps": {"a": {"rev": 1}, "b": {"rev": 1}}}})
    records = previous.value["_meta"]["stamps"]
    updated = document({"_meta": {"stamps": dict(records)}}, previous)
    assert updated.parts["_meta"].parts["stamps"] is previous.parts["_meta"].parts["stamps"]


@pytest.mark.feature("ledger-write-stamp-cost")
def test_reordered_stamp_records_store_the_new_order() -> None:
    previous = document({"_meta": {"stamps": {"a": {"rev": 1}, "b": {"rev": 1}}}})
    records = previous.value["_meta"]["stamps"]
    updated = document({"_meta": {"stamps": {"b": records["b"], "a": records["a"]}}}, previous)
    stamps = updated.parts["_meta"].parts["stamps"]
    assert stamps is not previous.parts["_meta"].parts["stamps"]
    assert list(stamps.value) == list(stamps.parts) == ["b", "a"]
    assert encoded(updated) == '{"_meta":{"stamps":{"b":{"rev":1},"a":{"rev":1}}}}'


@pytest.mark.feature("ledger-write-stamp-cost")
def test_added_and_dropped_stamp_records_build_a_new_stamps_fragment() -> None:
    previous = document({"_meta": {"stamps": {"a": {"rev": 1}, "b": {"rev": 1}}}})
    records = previous.value["_meta"]["stamps"]
    added = document({"_meta": {"stamps": {**records, "c": {"rev": 2}}}}, previous)
    dropped = document({"_meta": {"stamps": {"a": records["a"]}}}, previous)
    assert added.value["_meta"]["stamps"] == {"a": {"rev": 1}, "b": {"rev": 1}, "c": {"rev": 2}}
    assert added.parts["_meta"].parts["stamps"].parts["a"] is previous.parts["_meta"].parts["stamps"].parts["a"]
    assert dropped.value["_meta"]["stamps"] == {"a": {"rev": 1}}
    assert dropped.parts["_meta"].parts["stamps"] is not previous.parts["_meta"].parts["stamps"]


@pytest.mark.feature("ledger-write-stamp-cost")
def test_stamp_records_after_a_scalar_stamps_value_are_all_encoded_fresh() -> None:
    previous = document({"_meta": {"stamps": "scalar"}})
    updated = document({"_meta": {"stamps": {"a": {"rev": 1}}}}, previous)
    stamps = updated.parts["_meta"].parts["stamps"]
    assert stamps.value == {"a": {"rev": 1}} and isinstance(stamps.parts, dict)
    assert stamps.parts["a"].signature is not None
