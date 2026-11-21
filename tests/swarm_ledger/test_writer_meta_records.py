"""A write's writer copy rebuild encodes and copies the members and api operation records it changed, never the whole
map."""

import copy
from collections.abc import Callable
from typing import Any, cast

import pytest

from scripts.swarm_ledger.repository import entry, fragments, writer_copy
from scripts.swarm_ledger.repository.fragments import Fragment, document, encoded, exported
from scripts.swarm_ledger.repository.rows import encode

Change = Callable[[dict[str, Any]], None]


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


def stored(key: str, kept: int) -> tuple[Fragment, dict[str, Any]]:
    """The fragments and writer copy a first write leaves for a ledger whose ``key`` map holds ``kept`` records."""
    records = {f"r{n}": {"seq": n, "last_seen": n, "claims": [f"t{n}"]} for n in range(kept)}
    state = {"_meta": {"rev": 1, "stamps": {}, key: records}, "tasks": [{"id": "t0", "state": "open"}]}
    return writer_copy.written(state, None)


def seen(records: dict[str, Any]) -> None:
    """A member write: the writer's own record gets a new last seen time in place."""
    records["r1"]["last_seen"] = 99


def receipt(records: dict[str, Any]) -> None:
    """An API write: one receipt slot of the ring is replaced."""
    records["r1"] = {"seq": 1, "last_seen": 1, "claims": [], "digest": "d"}


def thaws_in_written(key: str, kept: int, change: Change, monkeypatch: pytest.MonkeyPatch) -> int:
    """The containers one write's writer copy rebuild copies after ``change`` touches one record of ``kept``."""
    published, writer = stored(key, kept)
    writer["_meta"]["rev"] = 2
    change(writer["_meta"][key])
    counted: list[object] = []
    thawed = entry.thawed

    def counting(value: object) -> object:
        counted.append(value)
        return thawed(value)

    with monkeypatch.context() as patch:
        patch.setattr(entry, "thawed", counting)
        patch.setattr(writer_copy, "thawed", counting)
        writer_copy.written(writer, published)
    return len(counted)


CASES = [("members", seen), ("api_operations", receipt)]


@pytest.mark.feature("ledger-write-meta-record-cost")
@pytest.mark.parametrize(("key", "change"), CASES)
def test_a_write_that_changes_one_record_copies_as_much_on_four_thousand_records_as_on_two_thousand(
    key: str, change: Change, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert thaws_in_written(key, 4000, change, monkeypatch) == thaws_in_written(key, 2000, change, monkeypatch)


@pytest.mark.feature("ledger-write-meta-record-cost")
@pytest.mark.parametrize(("key", "change"), CASES)
def test_the_rebuilt_writer_copy_stores_what_a_fresh_encoding_of_the_written_state_stores(
    key: str, change: Change
) -> None:
    published, writer = stored(key, 50)
    writer["_meta"]["rev"] = 2
    change(writer["_meta"][key])
    expected = copy.deepcopy(writer)
    rebuilt, held = writer_copy.written(writer, published)
    assert rebuilt.value == held == expected
    assert encoded(rebuilt) == encode(expected) == encoded(document(expected))
    assert exported(rebuilt) == encode(expected).encode()
    records, kept = child(rebuilt, "_meta", key), child(published, "_meta", key)
    assert child(records, "r1") is not child(kept, "r1")
    assert all(child(records, name) is child(kept, name) for name in mapped(kept) if name != "r1")
    assert held["_meta"][key] is not mapped(records)
    assert held["_meta"][key]["r1"] is not mapped(records)["r1"]
    assert held["_meta"][key]["r1"]["claims"] is not mapped(records)["r1"]["claims"]
    assert all(held["_meta"][key][name] is not mapped(records)[name] for name in mapped(kept))


@pytest.mark.feature("ledger-write-meta-record-cost")
@pytest.mark.parametrize("key", ["members", "api_operations"])
def test_unchanged_records_keep_the_stored_map_fragment(key: str) -> None:
    published, writer = stored(key, 3)
    rebuilt = document(writer, published)
    assert child(rebuilt, "_meta", key) is child(published, "_meta", key)


@pytest.mark.feature("ledger-write-meta-record-cost")
@pytest.mark.parametrize("key", ["members", "api_operations"])
def test_records_after_a_map_stored_whole_are_all_encoded_per_record(key: str) -> None:
    records = {"a": {"seq": 1}, "b": {"seq": 2}}
    rebuilt = fragments.meta_part(key, copy.deepcopy(records), Fragment(records))
    assert mapped(rebuilt) == records and isinstance(rebuilt.parts, dict)
    assert list(cast(dict[str, Fragment], rebuilt.parts)) == ["a", "b"]
    assert all(child(rebuilt, name).signature is not None for name in records)


@pytest.mark.feature("ledger-write-meta-record-cost")
@pytest.mark.parametrize("value", [["a", "b"], "scalar", None, 3])
def test_a_members_value_that_is_no_map_stays_one_leaf(value: object) -> None:
    rebuilt = fragments.meta_part("members", value, None)
    assert rebuilt.parts is None and rebuilt.value == value and rebuilt.signature is not None


@pytest.mark.feature("ledger-write-meta-record-cost")
def test_other_meta_maps_stay_one_leaf() -> None:
    rebuilt = fragments.meta_part("join_history", {"a": [1], "b": [2]}, None)
    assert rebuilt.parts is None and rebuilt.signature is not None
