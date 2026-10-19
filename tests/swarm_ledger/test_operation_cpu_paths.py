import copy
from unittest.mock import Mock

import pytest

from scripts.swarm_ledger.repository import fragments, rows


@pytest.mark.parametrize("value", [[1, 2, 1], [{"id": "one"}, {"id": "two"}, {"id": "one"}]])
def test_common_list_identities_need_no_json_encoding(value, monkeypatch):
    encode = Mock(wraps=rows.encode)
    monkeypatch.setattr(rows, "encode", encode)
    children = list(rows.children(value, ["tasks"], "resources"))
    assert [child[0] for child in children] == value
    assert [child[1][-1] for child in children] == (
        [["index", 0, 0], ["index", 1, 0], ["index", 2, 0]]
        if value == [1, 2, 1]
        else [["id", "one", 0], ["id", "two", 0], ["id", "one", 1]]
    )
    encode.assert_not_called()


@pytest.mark.parametrize("value", [[1, 2], [{"id": "one"}, {"id": "two"}]])
def test_common_list_paths_are_compared_without_serializing(value, monkeypatch):
    old = list(rows.children(value, ["tasks"], "resources"))
    new = list(rows.children([*value, 3], ["tasks"], "resources"))
    assert rows._list_paths(old) == [child[1][-1] for child in old]
    encode = Mock(wraps=rows.encode)
    monkeypatch.setattr(rows, "encode", encode)
    assert rows._child_paths_match(value, [*value, 3], old, new)
    encode.assert_not_called()


@pytest.mark.parametrize("value", [[{"id": 1}], [{"id": True}]])
def test_nonstring_id_paths_keep_the_encoded_prefix_match(value):
    old = list(rows.children(value, ["tasks"], "resources"))
    new = list(rows.children([*value, {"id": 2}], ["tasks"], "resources"))
    assert rows._list_paths(old) is None
    assert rows._child_paths_match(value, [*value, {"id": 2}], old, new)
    changed = list(rows.children([{"id": "different"}], ["tasks"], "resources"))
    assert not rows._child_paths_match(value, [{"id": "different"}], old, changed)


def test_string_dictionary_prefixes_use_the_direct_path_comparison():
    assert rows._dict_paths_match({"first": 1}, {"first": 2, "second": 3})
    assert not rows._dict_paths_match({"first": 1}, {"second": 3, "first": 2})
    assert not rows._dict_paths_match({1: 1}, {1: 2})
    assert not rows._dict_paths_match({}, {1: 2})


def test_list_diff_keeps_the_direct_path_comparison(monkeypatch):
    old = [{"id": "one", "value": 1}]
    new = [*old, {"id": "two", "value": 2}]
    match = rows._child_paths_match
    encode = Mock(wraps=rows.encode)

    def compare(left, right, old_children, new_children):
        calls = encode.call_count
        result = match(left, right, old_children, new_children)
        assert encode.call_count == calls
        return result

    monkeypatch.setattr(rows, "encode", encode)
    monkeypatch.setattr(rows, "_child_paths_match", compare)
    before, after = rows.diff(old, new)
    assert not before
    assert after


def test_dictionary_field_edits_do_not_enumerate_unchanged_subtrees(monkeypatch):
    shared = {"nested": [1, 2, 3]}
    old = {"keep": shared, "change": 1}
    new = {"keep": shared, "change": 2}
    children = Mock(wraps=rows.children)
    monkeypatch.setattr(rows, "children", children)
    before, after = rows.diff(old, new)
    assert list(before) == list(after) == ['["change"]']
    assert [call.args[0] for call in children.call_args_list] == [1, 2]


def test_dictionary_walk_skips_shared_containers_and_visits_shared_floats():
    shared = {"nested": [1, 2, 3]}
    number = float("nan")
    walk = Mock()
    rows._walk_dictionary(
        {"keep": shared, "number": number}, {"keep": shared, "number": number}, [], "fields", walk, {}
    )
    assert walk.call_count == 1
    assert walk.call_args.args[:2] == (number, number)
    assert walk.call_args.args[2:] == (["number"], "[]", "number", 1, "fields")


def test_noncanonical_float_signatures_reuse_committed_rows():
    state = {"tasks": [{"id": "one", "value": 1e20}]}
    old = fragments.document(state)
    new = fragments.document(copy.deepcopy(state), old)
    assert new.value["tasks"][0] is old.value["tasks"][0]
    assert fragments.encoded(new) == rows.encode(state)


def test_operation_relay_borrows_the_committed_state(tmp_path, monkeypatch):
    from dataclasses import replace

    from scripts.swarm_ledger.api import mutations
    from tests.swarm_ledger import operations_cpu_replay as replay
    from tests.swarm_ledger.test_operations_cpu_contract import small_corpus

    apply = mutations.apply
    observed = []

    def check_borrowed(services, slug, principal, payload):
        relay = services.ledger.relay_to_inbox

        def relay_borrowed(slug, state):
            assert state is services.repository.current(slug).state
            return relay(slug, state)

        borrowing = replace(services, ledger=replace(services.ledger, relay_to_inbox=relay_borrowed))
        reply = apply(borrowing, slug, principal, payload)
        observed.append(reply)
        return reply

    monkeypatch.setattr(mutations, "apply", check_borrowed)
    monkeypatch.setattr(replay, "corpus", small_corpus)
    report = replay.replay(tmp_path, monkeypatch)
    assert len(observed) == report["requests"] == 2
    assert observed[0]["applied"] == ["accepted"]
    assert observed[1]["rejected"] == ["missing"]
