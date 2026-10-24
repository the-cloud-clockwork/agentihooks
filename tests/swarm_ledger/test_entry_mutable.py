import json
from unittest.mock import Mock

from scripts.swarm_ledger.repository import entry
from scripts.swarm_ledger.repository.entry import Entry, thawed
from scripts.swarm_ledger.repository.rows import encode


def test_a_mutable_copy_equals_the_json_round_trip_and_shares_no_container():
    nested = {"k": [{"z": 1}]}
    state = {"tasks": [{"id": "t1", "n": 2**70, "f": 1.5, "b": True, "x": None}], "d": nested, "_meta": {"rev": 3}}
    copied = Entry(1, None, state).mutable()
    assert copied == json.loads(encode(state))
    assert copied is not state and copied["d"] is not nested and copied["d"]["k"] is not nested["k"]
    assert copied["d"]["k"][0] is not nested["k"][0]
    assert copied["tasks"][0]["id"] is state["tasks"][0]["id"]
    copied["tasks"][0]["id"] = "changed"
    assert state["tasks"][0]["id"] == "t1"


def test_plain_json_values_copy_without_encoding(monkeypatch):
    encoder = Mock(wraps=encode)
    monkeypatch.setattr(entry, "encode", encoder)
    assert thawed({"a": [1, "s", 2.5, False, None, {"b": []}]}) == {"a": [1, "s", 2.5, False, None, {"b": []}]}
    encoder.assert_not_called()


def test_values_a_json_round_trip_changes_come_back_as_it_gives_them():
    marker = type("Text", (str,), {})("t")
    state = {"pair": (1, [2]), "keys": {1: "one", "s": "two"}, "kind": marker, "deep": [{2.5: "x"}]}
    copied = thawed(state)
    assert copied == {"pair": [1, [2]], "keys": {"1": "one", "s": "two"}, "kind": "t", "deep": [{"2.5": "x"}]}
    assert type(copied["kind"]) is str and type(copied["pair"]) is list
