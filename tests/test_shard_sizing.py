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


def test_a_unit_joins_the_shard_that_already_collects_its_file_when_the_costs_tie() -> None:
    seconds = {"a": 0.0, "b": 0.0}
    serial = {"a": 0.1, "b": 0.0}
    collection = {"a": 0.1, "b": 0.2}
    files = {"a": "g", "b": "g"}
    assert assign_work(seconds, serial, collection, 2, 1, None, files) == [["a", "b"], []]


def test_a_shard_that_collects_two_files_carries_both_collection_costs() -> None:
    seconds = {"a": 8.0, "b": 4.8, "c": 0.5, "d": 0.1}
    collection = {"a": 1.0, "b": 1.0, "c": 3.0, "d": 0.1}
    serial = dict.fromkeys(seconds, 0.0)
    assert assign_work(seconds, serial, collection, 2, 1) == [["a", "d"], ["b", "c"]]


def test_a_shard_without_a_units_serial_group_adds_no_serial_time() -> None:
    seconds = {"a": 0.5, "b": 0.4}
    zero = dict.fromkeys(seconds, 0.0)
    assert assign_work(seconds, zero, zero, 2, 1) == [["a"], ["b"]]


def test_an_empty_shard_starts_with_no_parallel_load() -> None:
    seconds = {"a": 0.0, "b": 1.5}
    serial = {"a": 2.0, "b": 0.0}
    zero = dict.fromkeys(seconds, 0.0)
    assert assign_work(seconds, serial, zero, 2, 1) == [["a"], ["b"]]


def test_an_empty_shard_starts_with_no_serial_peak() -> None:
    seconds = {"p": 0.6, "q": 0.35, "d": 0.01}
    serial = dict.fromkeys(seconds, 0.0)
    collection = {"p": 0.1, "q": 0.0, "d": 0.3}
    files = {"p": "fb", "q": "fq", "d": "fb"}
    assert assign_work(seconds, serial, collection, 2, 1, None, files) == [["p"], ["q", "d"]]


def test_a_shards_first_serial_unit_adds_only_its_own_serial_time() -> None:
    seconds = {"a": 2.7, "c": 0.1, "b": 0.5}
    serial = {"a": 0.0, "c": 1.5, "b": 0.8}
    zero = dict.fromkeys(seconds, 0.0)
    assert assign_work(seconds, serial, zero, 2, 1) == [["a"], ["c", "b"]]
