import json

import pytest

from scripts.swarm_ledger.repository.fragments import Fragment, document, encoded, leaf
from scripts.swarm_ledger.repository.rows import encode


@pytest.mark.parametrize(
    "state",
    [
        {},
        {"_meta": {"events": [], "stamps": {}}, "tasks": []},
        {"tasks": [{"id": "é𝄞", "a": True, "b": 1, "c": 1.0, "d": -0.0, "text": '\n"\\'}]},
        {"_meta": {"events": [{"rev": 1}, {"rev": 1}], "stamps": {"a": {"rev": 1, "at": 2, "by": "writer"}}}},
        {"_meta": {"events": "scalar", "stamps": []}, "unknown": [None, [], {}, "é", 1.0]},
    ],
)
def test_generation_text_matches_canonical_encoding(state):
    first = document(state)
    second = document(json.loads(encode(state)), first)
    assert encoded(first) == encoded(second) == encode(state)
    assert first.value == state
    assert second is first


def test_generation_reuses_unchanged_rows_and_exact_number_types():
    old = document({"tasks": [{"id": "first", "value": True}, {"id": "second", "value": 0.0}]})
    new = document({"tasks": [{"id": "first", "value": 1}, {"id": "second", "value": 0.0}]}, old)
    assert type(new.value["tasks"][0]["value"]) is int
    assert new.value["tasks"][0] is not old.value["tasks"][0]
    assert new.value["tasks"][1] is old.value["tasks"][1]
    assert encoded(new) == encode(new.value)
    assert type(old.value["tasks"][0]["value"]) is bool


def test_generation_removal_and_reordering_do_not_keep_obsolete_fragments():
    first = document({"title": "first", "tasks": [{"id": "one"}, {"id": "two"}], "_meta": {"stamps": {"a": 1}}})
    changed = {"_meta": {"stamps": {}}, "tasks": [{"id": "two"}, {"id": "one"}, {"id": "three"}]}
    second = document(changed, first)
    third = document({"tasks": []}, second)
    assert encoded(second) == encode(changed)
    assert encoded(third) == encode({"tasks": []})
    assert first.value["_meta"]["stamps"] == {"a": 1}


def test_metadata_generations_share_unchanged_events_and_stamps():
    first = document({"_meta": {"rev": 1, "events": [{"rev": 1}], "stamps": {"a": {"rev": 1}, "b": {"rev": 1}}}})
    state = {"_meta": {"rev": 2, "events": [{"rev": 1}, {"rev": 2}], "stamps": {"a": {"rev": 1}, "b": {"rev": 2}}}}
    second = document(state, first)
    assert second.value["_meta"]["events"][0] is first.value["_meta"]["events"][0]
    assert second.value["_meta"]["stamps"]["a"] is first.value["_meta"]["stamps"]["a"]
    assert second.value["_meta"]["stamps"]["b"] is not first.value["_meta"]["stamps"]["b"]
    assert first.value["_meta"]["events"] == [{"rev": 1}]
    assert first.value["_meta"]["stamps"]["b"] == {"rev": 1}
    assert encoded(second) == encode(state)


def test_generation_rejects_nonfinite_values_before_identity_reuse():
    with pytest.raises(ValueError):
        document({"tasks": [{"value": float("nan")}]})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_native_null_encoding_cannot_reuse_a_finite_fragment(value):
    previous = document({"tasks": [{"value": None}]})
    with pytest.raises(ValueError):
        document({"tasks": [{"value": value}]}, previous)


def test_large_integers_keep_canonical_precision_in_the_fallback():
    first = document({"tasks": [{"value": 2**100}]})
    same = document({"tasks": [{"value": 2**100}]}, first)
    changed = document({"tasks": [{"value": 2**100 + 1}]}, same)
    assert same is first
    assert encoded(changed) == encode(changed.value)
    assert changed.value["tasks"][0]["value"] == 2**100 + 1


def test_fallback_signature_preserves_changed_number_types():
    old = document({"tasks": [{"large": 2**100, "value": True}]})
    new = document({"tasks": [{"large": 2**100, "value": 1}]}, old)
    assert type(new.value["tasks"][0]["value"]) is int
    assert new.value["tasks"][0] is not old.value["tasks"][0]
    assert encoded(new) == encode(new.value)
    assert type(old.value["tasks"][0]["value"]) is bool


def test_scalar_metadata_remains_a_leaf():
    value = {"_meta": "scalar"}
    result = document(value)
    assert result.value == value
    assert encoded(result) == encode(value)


def test_opaque_document_fields_reuse_unchanged_container_generations():
    def state(rev):
        return {
            "layout": {"widths": [10, 20]},
            "_meta": {"rev": rev, "api_operations": {"done": {"results": {"item": True}}}},
        }

    old = document(state(1))
    new = document(state(2), old)
    assert new.value["layout"] is old.value["layout"]
    assert new.value["_meta"]["api_operations"] is old.value["_meta"]["api_operations"]
    assert encoded(new) == encode(state(2))


def held(fragment):
    yield fragment
    parts = fragment.parts.values() if isinstance(fragment.parts, dict) else fragment.parts or ()
    for part in parts:
        yield from held(part)


def test_no_fragment_keeps_encoded_text_and_the_document_is_encoded_when_read():
    state = {"title": "é", "tasks": [{"id": "one"}], "_meta": {"events": [{"rev": 1}], "stamps": {"a": {"rev": 1}}}}
    root = document(state)
    fragments = list(held(root))
    assert len(fragments) == 9
    names = [name for name in Fragment.__slots__ if name != "value"]
    assert [name for item in fragments for name in names if isinstance(getattr(item, name), str)] == []
    assert encoded(root) == encode(state)
    assert encoded(root.parts["tasks"].parts[0]) == encode({"id": "one"})


def test_a_container_replacing_a_scalar_or_starting_empty_is_its_own_value():
    previous = document({"tasks": "scalar", "_meta": "scalar"})
    state = {"tasks": [], "_meta": {}}
    replaced = document(state, previous)
    assert replaced.value == state
    assert encoded(replaced) == encode(state)
    assert document({"tasks": []}).value == {"tasks": []}


def test_a_reordered_mapping_with_the_same_rows_is_rebuilt():
    first = document({"_meta": {"stamps": {"a": {"rev": 1}, "b": {"rev": 2}}}})
    state = {"_meta": {"stamps": {"b": {"rev": 2}, "a": {"rev": 1}}}}
    second = document(state, first)
    assert list(second.value["_meta"]["stamps"]) == ["b", "a"]
    assert encoded(second) == encode(state)
    assert second.value["_meta"]["stamps"]["a"] is first.value["_meta"]["stamps"]["a"]


def test_a_leaf_is_encoded_canonically_and_its_signature_tells_number_spellings_apart():
    assert encoded(leaf(1e-7)) == "1e-07"
    signatures = [leaf(1).signature, leaf(1.0).signature, leaf(True).signature]
    assert len(set(signatures)) == 3
    assert leaf({"a": 1, "b": 2}).signature != leaf({"b": 2, "a": 1}).signature
