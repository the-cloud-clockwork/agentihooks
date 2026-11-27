"""A write's writer copy rebuild costs the stamp records it replaced, never every stamp record the ledger holds."""

import sys
from typing import Any, cast

import pytest

from scripts.swarm_ledger.repository import fragments, writer_copy
from scripts.swarm_ledger.repository.fragments import Fragment, document, encoded

T0 = "tasks/t0/done"


def child(fragment: Fragment, *keys: str | int) -> Fragment:
    """The fragment that ``keys`` name under ``fragment``, a key for a mapping and an index for a list."""
    for key in keys:
        parts = cast(Any, fragment.parts)
        assert parts is not None
        fragment = parts[key]
    return fragment


def mapped(fragment: Fragment) -> dict[str, Any]:
    """The mapping ``fragment`` holds."""
    assert isinstance(fragment.value, dict)
    return cast(dict[str, Any], fragment.value)


def stamped(records: dict[str, Any]) -> Fragment:
    """The fragments of a ledger whose only metadata is ``records``."""
    return document({"_meta": {"stamps": records}})


def stored(kept: int) -> tuple[Fragment, dict[str, Any]]:
    """The fragments and writer copy a first write leaves for a ledger holding ``kept`` stamp records."""
    records = {f"tasks/t{n}/done": {"at": n, "rev": 1, "by": "swarm"} for n in range(kept)}
    state = {"_meta": {"rev": 1, "stamps": records}, "tasks": [{"id": "t0", "state": "open"}, {"id": "t1"}]}
    return writer_copy.written(state, None)


def replace_one(writer: dict[str, Any]) -> None:
    """Change one task and replace its stamp record the way a task write does."""
    writer["_meta"]["rev"] = 2
    writer["tasks"][0]["state"] = "claimed"
    writer["_meta"]["stamps"][T0] = {"at": 99, "rev": 2, "by": "engineer"}


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
    assert held["tasks"][0] is not mapped(rebuilt)["tasks"][0]
    assert held["tasks"][1] is not mapped(rebuilt)["tasks"][1]
    stamps, kept = child(rebuilt, "_meta", "stamps"), child(published, "_meta", "stamps")
    assert child(stamps, T0) is not child(kept, T0)
    assert all(child(stamps, key) is child(kept, key) for key in mapped(kept) if key != T0)
    assert child(rebuilt, "tasks", 1) is child(published, "tasks", 1)


@pytest.mark.feature("ledger-write-stamp-cost")
def test_kept_stamp_records_reuse_their_fragments_without_encoding_them_again(monkeypatch: pytest.MonkeyPatch) -> None:
    previous = stamped({"a": {"rev": 1}, "b": {"rev": 1}, "c": {"rev": 1}})
    records = mapped(child(previous, "_meta", "stamps"))
    leaves: list[object] = []
    leaf = fragments.leaf

    def counted(value: object, known: Fragment | None = None) -> Fragment:
        leaves.append(value)
        return leaf(value, known)

    monkeypatch.setattr(fragments, "leaf", counted)
    updated = document({"_meta": {"stamps": {**records, "b": {"rev": 2}}}}, previous)
    assert [value for value in leaves if isinstance(value, dict) and "rev" in value] == [{"rev": 2}]
    assert mapped(child(updated, "_meta", "stamps")) == {"a": {"rev": 1}, "b": {"rev": 2}, "c": {"rev": 1}}
    assert child(updated, "_meta", "stamps", "a") is child(previous, "_meta", "stamps", "a")


@pytest.mark.feature("ledger-write-stamp-cost")
def test_unchanged_stamp_records_keep_the_stored_stamps_fragment() -> None:
    previous = stamped({"a": {"rev": 1}, "b": {"rev": 1}})
    records = mapped(child(previous, "_meta", "stamps"))
    updated = document({"_meta": {"stamps": dict(records)}}, previous)
    assert child(updated, "_meta", "stamps") is child(previous, "_meta", "stamps")


@pytest.mark.feature("ledger-write-stamp-cost")
def test_a_replaced_record_equal_to_the_stored_one_keeps_the_stored_stamps_fragment() -> None:
    previous = stamped({"a": {"rev": 1}, "b": {"rev": 1}})
    records = mapped(child(previous, "_meta", "stamps"))
    updated = document({"_meta": {"stamps": {**records, "b": {"rev": 1}}}}, previous)
    assert child(updated, "_meta", "stamps") is child(previous, "_meta", "stamps")


@pytest.mark.feature("ledger-write-stamp-cost")
def test_reordered_stamp_records_store_the_new_order() -> None:
    previous = stamped({"a": {"rev": 1}, "b": {"rev": 1}})
    records = mapped(child(previous, "_meta", "stamps"))
    updated = document({"_meta": {"stamps": {"b": records["b"], "a": records["a"]}}}, previous)
    stamps = child(updated, "_meta", "stamps")
    assert stamps is not child(previous, "_meta", "stamps")
    assert list(mapped(stamps)) == list(cast(dict[str, Fragment], stamps.parts)) == ["b", "a"]
    assert encoded(updated) == '{"_meta":{"stamps":{"b":{"rev":1},"a":{"rev":1}}}}'


@pytest.mark.feature("ledger-write-stamp-cost")
def test_added_and_dropped_stamp_records_build_a_new_stamps_fragment() -> None:
    previous = stamped({"a": {"rev": 1}, "b": {"rev": 1}})
    records = mapped(child(previous, "_meta", "stamps"))
    added = document({"_meta": {"stamps": {**records, "c": {"rev": 2}}}}, previous)
    dropped = document({"_meta": {"stamps": {"a": records["a"]}}}, previous)
    assert mapped(child(added, "_meta", "stamps")) == {"a": {"rev": 1}, "b": {"rev": 1}, "c": {"rev": 2}}
    assert list(cast(dict[str, Fragment], child(added, "_meta", "stamps").parts)) == ["a", "b", "c"]
    assert child(added, "_meta", "stamps", "a") is child(previous, "_meta", "stamps", "a")
    assert mapped(child(dropped, "_meta", "stamps")) == {"a": {"rev": 1}}
    assert child(dropped, "_meta", "stamps") is not child(previous, "_meta", "stamps")


@pytest.mark.feature("ledger-write-stamp-cost")
def test_stamp_records_after_a_scalar_stamps_value_are_all_encoded_fresh() -> None:
    previous = document({"_meta": {"stamps": "scalar"}})
    updated = document({"_meta": {"stamps": {"a": {"rev": 1}}}}, previous)
    stamps = child(updated, "_meta", "stamps")
    assert mapped(stamps) == {"a": {"rev": 1}} and isinstance(stamps.parts, dict)
    assert child(stamps, "a").signature is not None


@pytest.mark.feature("ledger-write-stamp-cost")
def test_stamp_records_after_a_stamps_leaf_holding_a_mapping_are_all_encoded_fresh() -> None:
    records = {"a": {"rev": 1}, "b": {"rev": 1}}
    rebuilt = fragments.stamps(dict(records), Fragment(records))
    assert mapped(rebuilt) == records and isinstance(rebuilt.parts, dict)
    assert list(cast(dict[str, Fragment], rebuilt.parts)) == ["a", "b"]
    assert child(rebuilt, "a").signature is not None


@pytest.mark.feature("ledger-write-stamp-cost")
def test_replaced_names_the_changed_and_added_keys_or_none_when_the_stored_order_moved() -> None:
    first, second = {"rev": 1}, {"rev": 1}
    old = {"a": first, "b": second}
    assert fragments.replaced({"a": first, "b": second}, old) == []
    assert fragments.replaced({"a": first, "b": {"rev": 1}}, old) == ["b"]
    assert fragments.replaced({"a": {"rev": 1}, "b": second, "c": first}, old) == ["a", "c"]
    assert fragments.replaced({"b": second, "a": first}, old) is None
    assert fragments.replaced({"a": first}, old) is None
    assert fragments.replaced({"a": first}, {}) == ["a"]


@pytest.mark.feature("ledger-write-stamp-cost")
def test_a_write_leaves_the_stored_fragments_and_the_state_it_started_from_unchanged() -> None:
    published, writer = stored(50)
    replace_one(writer)
    writer["_meta"]["stamps"]["tasks/t99/done"] = {"at": 100, "rev": 2, "by": "engineer"}
    stamps = child(published, "_meta", "stamps")
    kept_parts, kept_values = dict(cast(dict[str, Fragment], stamps.parts)), dict(mapped(stamps))
    built = document(writer, published)
    built_parts, built_values = dict(cast(dict[str, Fragment], built.parts)), dict(mapped(built))
    built_meta = dict(cast(dict[str, Fragment], child(built, "_meta").parts))
    rebuilt, copied = writer_copy.detached(built, published, writer)
    held = cast(dict[str, Any], copied)
    assert cast(dict[str, Fragment], stamps.parts) == kept_parts and mapped(stamps) == kept_values
    assert all(cast(dict[str, Fragment], stamps.parts)[key] is part for key, part in kept_parts.items())
    assert all(cast(dict[str, Fragment], built.parts)[key] is part for key, part in built_parts.items())
    assert all(mapped(built)[key] is value for key, value in built_values.items())
    assert all(cast(dict[str, Fragment], child(built, "_meta").parts)[key] is part for key, part in built_meta.items())
    assert held is not writer and held["_meta"] is not writer["_meta"]
    assert rebuilt.value == held == writer


@pytest.mark.feature("ledger-write-stamp-cost")
def test_a_write_that_reorders_the_ledger_and_its_stamp_records_stores_the_new_order_in_both_copies() -> None:
    published, writer = stored(5)
    replace_one(writer)
    records = writer["_meta"]["stamps"]
    writer["_meta"]["stamps"] = dict(reversed(list(records.items())))
    reordered = {"tasks": writer["tasks"], "_meta": writer["_meta"]}
    rebuilt, held = writer_copy.written(reordered, published)
    assert list(mapped(rebuilt)) == list(held) == ["tasks", "_meta"]
    assert list(mapped(child(rebuilt, "_meta", "stamps"))) == list(reversed(list(records)))
    assert rebuilt.value == held == reordered
    assert encoded(rebuilt) == encoded(document(reordered))
    assert held["tasks"][0] is not mapped(rebuilt)["tasks"][0]
    assert held["tasks"][0] is not reordered["tasks"][0]
