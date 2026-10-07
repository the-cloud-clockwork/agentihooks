import copy
import json
import random

import pytest

from scripts.swarm_ledger.events import patch

CASES = [
    (1, 1),
    (1, 2),
    (None, {"a": 1}),
    ({"a": 1}, None),
    ({"a": 1, "b": 2}, {"a": 1, "c": 3}),
    ({"a": {"b": {"c": 1}}}, {"a": {"b": {"c": 2}}}),
    ([], []),
    ([], [{"id": "a"}]),
    ([{"id": "a"}], []),
    ([1, 2, 3], [2, 3, 4]),
    ([1, 2, 3], [3, 2, 1]),
    ([1, 2], [1, 2, 3, 4]),
    ([{"id": "a", "v": 1}, {"id": "b"}], [{"id": "a", "v": 2}, {"id": "b"}, {"id": "c"}]),
    ([{"id": "a"}, {"id": "b"}, {"id": "c"}], [{"id": "c"}, {"id": "a", "x": 1}]),
    ([{"id": "a"}, {"id": "b"}], [{"id": "b", "x": 1}, {"id": "c"}]),
    ([{"id": "a"}, {"id": "a"}], [{"id": "a"}]),
    ([{"id": 1}], [{"id": 2}]),
    ([{"rev": 1}, {"rev": 2}], [{"rev": 2}, {"rev": 3}]),
    ({"x": [1]}, {"x": {"y": 1}}),
]


@pytest.mark.parametrize("old,new", CASES)
def test_applying_the_diff_of_two_values_rebuilds_the_new_value(old, new):
    before = copy.deepcopy(old)
    change = patch.diff(old, new)
    assert old == before
    if old == new:
        assert change is None
    else:
        assert patch.apply(old, json.loads(json.dumps(change))) == new
        assert old == before


def test_random_ledgers_round_trip():
    rng = random.Random(7)
    doc = {"tasks": [], "chat": [], "_meta": {"rev": 0, "events": []}}
    for rev in range(1, 200):
        new = copy.deepcopy(doc)
        new["_meta"]["rev"] = rev
        new["_meta"]["events"] = (new["_meta"]["events"] + [{"rev": rev, "kind": "x"}])[-20:]
        if rng.random() < 0.5:
            new["tasks"].append({"id": f"t{rev}", "state": "open"})
        if new["tasks"] and rng.random() < 0.5:
            rng.choice(new["tasks"])["state"] = rng.choice(["claimed", "done", "pr"])
        if new["tasks"] and rng.random() < 0.2:
            new["tasks"].pop(rng.randrange(len(new["tasks"])))
        if rng.random() < 0.2:
            rng.shuffle(new["tasks"])
        new["chat"] = (new["chat"] + [{"id": f"m{rev}", "text": "hi"}])[-15:]
        assert patch.apply(doc, patch.diff(doc, new)) == new
        doc = new


def test_an_object_patch_names_only_changed_added_and_removed_keys():
    assert patch.diff({"a": 1, "b": 2, "c": 3}, {"a": 1, "b": 5, "d": 4}) == {
        "o": {"b": {"v": 5}, "c": {"d": 1}, "d": {"v": 4}}
    }


def test_a_scalar_or_type_change_replaces_the_value():
    assert patch.diff(1, 2) == {"v": 2}
    assert patch.diff([1], {"a": 1}) == {"v": {"a": 1}}
    assert patch.diff({"a": 1}, [1]) == {"v": [1]}


def test_an_appended_log_sends_only_its_new_tail():
    old = [{"rev": 1}, {"rev": 2}, {"rev": 3}]
    assert patch.diff(old, old[1:] + [{"rev": 4}]) == {"drop": 1, "add": [{"rev": 4}], "u": []}
    assert patch.diff(old, old + [{"rev": 4}, {"rev": 5}]) == {"drop": 0, "add": [{"rev": 4}, {"rev": 5}], "u": []}


def test_a_log_without_overlap_drops_every_old_item():
    assert patch.diff([1, 2, 3], [2, 1]) == {"drop": 3, "add": [2, 1], "u": []}
    assert patch.diff([1, 2], [9]) == {"drop": 2, "add": [9], "u": []}
    assert patch.diff([1, 2], []) == {"drop": 2, "add": [], "u": []}


def test_overlap_finds_the_fewest_dropped_items():
    assert patch.overlap([1, 2, 3], [1, 2, 3, 4]) == 0
    assert patch.overlap([1, 2, 3], [2, 3]) == 1
    assert patch.overlap([1, 2, 3], [3, 9]) == 2
    assert patch.overlap([1, 2, 3], [7]) == 3
    assert patch.overlap([1, 1, 1], [1, 1]) == 1
    assert patch.overlap([], [5]) == 0
    assert patch.overlap([1, 2, 3], [9, 2, 3]) == 3
    assert patch.overlap([1, 2, 3], []) == 3
    assert patch.overlap([], []) == 0


def test_an_id_list_update_sends_only_the_changed_item():
    old = [{"id": "a", "v": 1}, {"id": "b", "v": 1}, {"id": "c", "v": 1}]
    new = [{"id": "a", "v": 1}, {"id": "b", "v": 2}, {"id": "c", "v": 1}]
    assert patch.diff(old, new) == {"drop": 0, "add": [], "u": [{"id": "b", "v": 2}]}


def test_an_id_list_trimmed_and_appended_sends_the_drop_and_the_new_items():
    old = [{"id": "a"}, {"id": "b"}]
    assert patch.diff(old, [{"id": "b"}, {"id": "c"}]) == {"drop": 1, "add": [{"id": "c"}], "u": []}


def test_a_reordered_id_list_sends_the_order_and_the_changed_items():
    old = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    new = [{"id": "c"}, {"id": "b", "x": 1}, {"id": "d"}]
    assert patch.diff(old, new) == {"ids": ["c", "b", "d"], "u": [{"id": "b", "x": 1}, {"id": "d"}]}


def test_an_id_list_keeping_less_than_half_its_new_items_in_place_sends_the_order():
    old = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    assert patch.diff(old, [{"id": "c"}, {"id": "d"}]) == {"drop": 2, "add": [{"id": "d"}], "u": []}
    assert patch.diff(old, [{"id": "c"}, {"id": "d"}, {"id": "e"}]) == {
        "ids": ["c", "d", "e"],
        "u": [{"id": "d"}, {"id": "e"}],
    }
    assert patch.diff([], [{"id": "a"}]) == {"ids": ["a"], "u": [{"id": "a"}]}
    assert patch.diff(old, []) == {"drop": 3, "add": [], "u": []}


def test_ids_count_only_when_every_item_has_a_unique_string_id():
    assert patch.ids_of([{"id": "a"}, {"id": "b"}]) == ["a", "b"]
    assert patch.ids_of([]) == []
    assert patch.ids_of([{"id": "a"}, {"id": "a"}]) is None
    assert patch.ids_of([{"id": "a"}, {"x": 1}]) is None
    assert patch.ids_of([{"id": 1}]) is None
    assert patch.ids_of(["a"]) is None


def test_apply_returns_new_values_and_leaves_the_old_one_alone():
    old = {"a": [{"id": "x", "v": 1}], "b": 1}
    new = patch.apply(old, {"o": {"a": {"drop": 0, "add": [], "u": [{"id": "x", "v": 2}]}, "b": {"d": 1}}})
    assert new == {"a": [{"id": "x", "v": 2}]}
    assert old == {"a": [{"id": "x", "v": 1}], "b": 1}


def test_apply_a_key_patch_to_a_missing_key():
    assert patch.apply({}, {"o": {"a": {"v": 1}, "b": {"d": 1}}}) == {"a": 1}


def test_apply_an_upsert_without_changes_keeps_the_kept_items():
    assert patch.apply([{"id": "a"}, {"id": "b"}], {"drop": 1, "add": [{"id": "c"}], "u": []}) == [
        {"id": "b"},
        {"id": "c"},
    ]
    assert patch.apply([1, 2, 3], {"drop": 2, "add": [4], "u": []}) == [3, 4]
