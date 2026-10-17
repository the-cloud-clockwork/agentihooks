import math

import pytest

import scripts.swarm_ledger.events.patch as patch


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ({"nested": [True]}, {"nested": [1]}),
        ({"nested": [1]}, {"nested": [1.0]}),
        ([{"value": False}], [{"value": 0}]),
        ([{"value": 0}], [{"value": 0.0}]),
        ([{"value": True}] * 16, [{"value": 1}] * 16),
        ([{"value": 1}] * 16, [{"value": 1.0}] * 16),
    ],
)
def test_nested_number_type_changes_remain_visible(old, new):
    change = patch.diff(old, new)
    assert change is not None
    result = patch.apply(old, change)
    assert patch.same(result, new)
    assert not patch.same(result, old)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ({"first": [0.0], "last": None}, {"last": None, "first": [-0.0]}),
        ([{"value": -0.0}], [{"value": 0.0}]),
        ([{"value": -0.0}] * 16, [{"value": 0.0}] * 16),
        ({"first": 1, "last": ["evidence"]}, {"last": ["evidence"], "first": 1}),
    ],
)
def test_equal_json_values_do_not_publish_a_patch(old, new):
    assert patch.diff(old, new) is None


def test_nonfinite_nested_values_keep_their_comparison_behavior():
    value = [math.nan] * 16
    assert not patch.same(value, value)
    assert patch.diff(value, value) is not None


def test_default_list_comparison_keeps_nonfinite_values_changed():
    value = [math.nan]
    assert patch.overlap(value, value) == 1
    change = patch.list_diff(value, value)
    assert change["drop"] == 1
    assert change["add"] == value
    assert change["u"] == []


def test_non_json_values_keep_the_legacy_comparison():
    old, new = [{1, 2}] * 16, [{2, 1}] * 16
    assert patch.diff(old, new) is None
