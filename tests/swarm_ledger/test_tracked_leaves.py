"""A tracked writer leaf holds the stored fragment it equals until any write method changes it or anything inside it."""

import copy
from collections.abc import Callable
from typing import Any, cast

import pytest

from scripts.swarm_ledger.repository import tracked
from scripts.swarm_ledger.repository.fragments import Fragment, document, leaf

Write = Callable[[Any], object]

DICT_READS = {
    "__class__", "__class_getitem__", "__contains__", "__delattr__", "__dir__", "__eq__", "__format__", "__ge__",
    "__getattribute__", "__getitem__", "__getstate__", "__gt__", "__hash__", "__init_subclass__", "__iter__", "__le__",
    "__len__", "__lt__", "__ne__", "__new__", "__or__", "__reduce__", "__reduce_ex__", "__repr__", "__reversed__",
    "__ror__", "__setattr__", "__sizeof__", "__str__", "__subclasshook__", "copy", "fromkeys", "get", "items", "keys",
    "values",
}  # fmt: skip
LIST_READS = {
    "__add__", "__class__", "__class_getitem__", "__contains__", "__delattr__", "__dir__", "__eq__", "__format__",
    "__ge__", "__getattribute__", "__getitem__", "__getstate__", "__gt__", "__hash__", "__init_subclass__", "__iter__",
    "__le__", "__len__", "__lt__", "__mul__", "__ne__", "__new__", "__reduce__", "__reduce_ex__", "__repr__",
    "__reversed__", "__rmul__", "__setattr__", "__sizeof__", "__str__", "__subclasshook__", "copy", "count", "index",
}  # fmt: skip
DICT_WRITES: list[Write] = [
    lambda value: value.__init__({"added": 1}),
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
    lambda value: value.__init__(["x"]),
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
    return leaf({"title": "kept", "due": None, "comments": ["b", "a"], "owner": {"title": "lead", "tags": ["b", "a"]}})


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
    assert list(value.items()) and list(value) and "title" in value and len(value) == 4
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
def test_every_dict_and_list_method_a_tracked_leaf_inherits_is_a_read() -> None:
    assert {name for name in dir(dict) if name not in vars(tracked.TrackedDict)} == DICT_READS
    assert {name for name in dir(list) if name not in vars(tracked.TrackedList)} == LIST_READS


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
def test_a_deep_copy_keeps_the_containers_a_writer_copy_shares() -> None:
    value = writer(stored())
    value["again"] = value["comments"]
    value["owners"] = [value["owner"], value["owner"]]
    doc = {"first": value, "second": value}
    deep = copy.deepcopy(doc)
    assert deep == doc
    assert deep["first"] is deep["second"]
    assert deep["first"]["again"] is deep["first"]["comments"]
    assert deep["first"]["owners"][0] is deep["first"]["owners"][1] is deep["first"]["owner"]
    assert type(deep["first"]["owners"]) is list


@pytest.mark.feature("ledger-write-partial-rebuild")
def test_write_methods_return_what_the_plain_methods_return() -> None:
    value = writer(stored())
    value["comments"] += ["c"]
    assert value["comments"] == ["b", "a", "c"]
    assert value["comments"].pop() == "c"
    assert value.pop("title") == "kept"
    assert value.setdefault("title", "new") == "new"
    assert value.popitem() == ("title", "new")
    tags = value["owner"]["tags"]
    tags *= 2
    assert value["owner"]["tags"] == ["b", "a", "b", "a"]
    owner = value["owner"]
    owner |= {"seat": "eng-1"}
    assert value["owner"]["seat"] == "eng-1"


@pytest.mark.feature("ledger-write-partial-rebuild")
def test_a_leaf_that_no_json_round_trip_keeps_is_bound_after_one() -> None:
    fragment = leaf({1: ["a"], "two": ("b", "c")})
    value = tracked.bound(fragment)
    assert value == {"1": ["a"], "two": ["b", "c"]}
    assert type(value) is tracked.TrackedDict
    assert tracked.held(value) is fragment
    cast(dict[str, Any], value)["two"].append("d")
    assert tracked.held(value) is None


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
