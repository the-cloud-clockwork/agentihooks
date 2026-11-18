"""The greedy shard split charges each file's collection once per shard and an absent serial group nothing."""

import pytest

from scripts.shard_sizing import assign_work

pytestmark = pytest.mark.unit


def test_a_unit_joins_the_shard_that_already_collects_its_file() -> None:
    seconds = {"a": 3.0, "b": 2.0, "c": 1.0}
    collection = {"a": 1.0, "b": 1.0, "c": 2.0}
    serial = dict.fromkeys(seconds, 0.0)
    files = {"a": "f", "b": "g", "c": "f"}
    assert assign_work(seconds, serial, collection, 2, 1, None, files) == [["a", "c"], ["b"]]


def test_a_shard_that_collects_two_files_carries_both_collection_costs() -> None:
    seconds = {"a": 8.0, "b": 4.8, "c": 0.5, "d": 0.1}
    collection = {"a": 1.0, "b": 1.0, "c": 3.0, "d": 0.1}
    serial = dict.fromkeys(seconds, 0.0)
    assert assign_work(seconds, serial, collection, 2, 1) == [["a", "d"], ["b", "c"]]


def test_a_shard_without_a_units_serial_group_adds_no_serial_time() -> None:
    seconds = {"a": 0.5, "b": 0.4}
    zero = dict.fromkeys(seconds, 0.0)
    assert assign_work(seconds, zero, zero, 2, 1) == [["a"], ["b"]]
