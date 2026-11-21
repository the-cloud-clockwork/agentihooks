"""A tracked writer leaf holds the stored fragment it equals until any write method changes it or anything inside it."""

import copy
from collections.abc import Callable
from typing import Any, cast

import pytest

from scripts.swarm_ledger.repository import tracked
from scripts.swarm_ledger.repository.fragments import Fragment, document, leaf

Write = Callable[[Any], object]

DICT_WRITES: list[Write] = [
    lambda value: value.__setitem__("title", "renamed"),
    lambda value: value.__delitem__("title"),
    lambda value: value.clear(),
    lambda value: value.pop("title"),
    lambda value: value.popitem(),
    lambda value: value.setdefault("added", 1),
    lambda value: value.update(added=1),
    lambda value: value.__ior__({"added": 1}),
]
LIST_WRITES: list[Write] = [
    lambda value: value.__setitem__(0, "x"),
    lambda value: value.__setitem__(slice(0, 1), ["x"]),
    lambda value: value.__delitem__(0),
    lambda value: value.append("x"),
    lambda value: value.extend(["x"]),
    lambda value: value.insert(0, "x"),
    lambda value: value.pop(),
    lambda value: value.remove("a"),
    lambda value: value.clear(),
    lambda value: value.sort(),
    lambda value: value.reverse(),
    lambda value: value.__iadd__(["x"]),
    lambda value: value.__imul__(2),
]


def stored() -> Fragment:
    """A stored task leaf with a nested comment list and a nested mapping."""
    return leaf({"title": "kept", "comments": ["b", "a"], "owner": {"name": "eng", "tags": ["b", "a"]}})


def writer(fragment: Fragment) -> dict[str, Any]:
    """The tracked writer copy bound to ``fragment``."""
    value = tracked.bound(fragment)
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)


@pytest.mark.feature("ledger-write-partial-rebuild")
def test_a_bound_copy_equals_its_fragment_shares_no_container_and_holds_it() -> None:
    fragment = stored()
    value = writer(fragment)
    assert value == fragment.value
    assert value is not fragment.value
    assert value["comments"] is not cast(dict[str, Any], fragment.value)["comments"]
    assert tracked.held(value) is fragment
    assert leaf(value, None) is fragment


@pytest.mark.feature("ledger-write-partial-rebuild")
def test_reads_leave_a_bound_copy_holding_its_fragment() -> None:
    fragment = stored()
    value = writer(fragment)
    assert value.get("title") == value["title"] == "kept"
    assert list(value.items()) and list(value) and "title" in value and len(value) == 3
    assert value.copy() == value and value["comments"].copy() == ["b", "a"]
    assert value["comments"][0:1] == ["b"] and value["comments"].index("a") == 1
    assert value.setdefault("title", "other") == "kept"
    assert value.pop("missing", None) is None
    with pytest.raises(KeyError):
        value.pop("missing")
    assert tracked.held(value) is fragment


@pytest.mark.feature("ledger-write-partial-rebuild")
@pytest.mark.parametrize("write", DICT_WRITES)
def test_every_dict_write_method_on_the_leaf_or_inside_it_drops_its_fragment(write: Write) -> None:
    value = writer(stored())
    write(value)
    assert tracked.held(value) is None
    nested = writer(stored())
    write(nested["owner"])
    assert tracked.held(nested) is None


@pytest.mark.feature("ledger-write-partial-rebuild")
@pytest.mark.parametrize("write", LIST_WRITES)
def test_every_list_write_method_inside_the_leaf_drops_its_fragment(write: Write) -> None:
    value = writer(stored())
    write(value["comments"])
    assert tracked.held(value) is None
    deeper = writer(stored())
    write(deeper["owner"]["tags"])
    assert tracked.held(deeper) is None


@pytest.mark.feature("ledger-write-partial-rebuild")
def test_a_list_leaf_drops_its_fragment_on_a_write_and_keeps_it_on_reads() -> None:
    fragment = leaf([{"rev": 1}, {"rev": 2}])
    value = tracked.bound(fragment)
    assert isinstance(value, list)
    assert sorted(item["rev"] for item in value) == [1, 2]
    assert tracked.held(value) is fragment
    value[1]["rev"] = 3
    assert tracked.held(value) is None


@pytest.mark.feature("ledger-write-partial-rebuild")
def test_a_nested_container_never_claims_a_fragment_and_moving_it_drops_the_leaf_it_enters() -> None:
    source, target = writer(stored()), writer(stored())
    moved = source["owner"]
    assert tracked.held(moved) is None
    assert leaf(moved, None) is not None and tracked.held(source) is not None
    target["moved"] = moved
    assert tracked.held(target) is None
    assert tracked.held(source) is not None


@pytest.mark.feature("ledger-write-partial-rebuild")
def test_copies_of_a_tracked_leaf_are_plain() -> None:
    value = writer(stored())
    for made in (copy.copy(value), copy.deepcopy(value), value.copy(), {**value}):
        assert type(made) is dict
        assert tracked.held(made) is None
    deep = copy.deepcopy(value)
    assert type(deep["comments"]) is list and type(deep["owner"]) is dict and type(deep["owner"]["tags"]) is list
    assert deep == value
    deep["owner"]["tags"].append("c")
    assert tracked.held(value) is not None
    assert type(copy.copy(value["comments"])) is list and type(value["comments"][:]) is list


@pytest.mark.feature("ledger-write-partial-rebuild")
def test_a_scalar_leaf_copy_is_the_scalar_and_holds_nothing() -> None:
    fragment = leaf("plain text")
    assert tracked.bound(fragment) == "plain text"
    assert tracked.held("plain text") is None
    assert tracked.held({"title": "kept"}) is None


@pytest.mark.feature("ledger-write-partial-rebuild")
def test_a_writer_copy_of_a_document_keeps_its_structure_plain_and_its_leaves_bound() -> None:
    fragments = document(
        {"_meta": {"rev": 1, "stamps": {"a": {"rev": 1}}, "events": [{"rev": 1}]}, "tasks": [{"id": "t1"}]}
    )
    copied = tracked.copied(fragments)
    assert copied == fragments.value
    assert type(copied) is dict and type(copied["tasks"]) is list and type(copied["_meta"]) is dict
    parts = cast(dict[str, Fragment], fragments.parts)
    tasks = cast(tuple[Fragment, ...], parts["tasks"].parts)
    assert tracked.held(copied["tasks"][0]) is tasks[0]
    assert tracked.held(copied["_meta"]["events"][0]) is not None
