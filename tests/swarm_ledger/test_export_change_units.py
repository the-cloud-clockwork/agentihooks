import io
import json
import urllib.error

import pytest

from scripts.swarm_ledger.api import client as api
from scripts.swarm_ledger.api.routes import export_changes

NEW = {
    "overview": "o",
    "summary": {"a": 1},
    "tasks": [{"id": "t1", "s": 2}, {"id": "t2", "s": 1}],
    "_meta": {"rev": 3, "stamps": {"a": 1, "b": 2}, "events": [{"e": 1}], "seeds": {"x": 1}, "api_operations": [1]},
}
OLD = {
    "overview": "x",
    "gone": 1,
    "summary": {"a": 1, "b": 2},
    "tasks": [{"id": "t2", "s": 0}, {"id": "t9"}, {"id": "t1", "s": 2}],
    "_meta": {"rev": 2, "stamps": {"a": 0, "b": 2}, "events": []},
}
PATHS = {
    '["tasks",["id","t2",0],"s"]',
    '["tasks",["id","t9",0]]',
    '["overview"]',
    '["gone"]',
    '["absent"]',
    '["summary","b"]',
    '["_meta","stamps","a"]',
    '["_meta","rev"]',
    '["_meta","seeds","x"]',
    '["_meta","api_operations",["index",0,0]]',
}
VIEW = {**NEW, "_meta": {"rev": 3, "stamps": {"a": 1, "b": 2}, "events": [{"e": 1}]}}


def test_changes_name_each_changed_item_once():
    assert json.loads(export_changes(NEW, PATHS)) == {
        "changes": {
            "set": [
                [["_meta", "events"], [{"e": 1}]],
                [["_meta", "rev"], 3],
                [["_meta", "stamps", "a"], 1],
                [["overview"], "o"],
            ],
            "drop": [["absent"], ["gone"], ["summary", "b"]],
            "lists": {
                "tasks": {"order": ['["id","t1",0]', '["id","t2",0]'], "items": {'["id","t2",0]': {"id": "t2", "s": 1}}}
            },
        }
    }


def test_a_changed_container_carries_its_changed_children():
    paths = {'["_meta","stamps"]', '["_meta","stamps","a"]', '["summary"]', '["summary","b"]'}
    assert json.loads(export_changes(NEW, paths))["changes"] == {
        "set": [[["_meta", "events"], [{"e": 1}]], [["_meta", "stamps"], {"a": 1, "b": 2}], [["summary"], {"a": 1}]],
        "drop": [],
        "lists": {},
    }


def test_stamps_change_per_stamp_and_other_meta_per_key():
    state = {**NEW, "_meta": {**NEW["_meta"], "members": {"x": {"role": "r"}}}}
    paths = {'["_meta","stamps","a","deep"]', '["_meta","members","x","role"]'}
    assert json.loads(export_changes(state, paths))["changes"]["set"] == [
        [["_meta", "events"], [{"e": 1}]],
        [["_meta", "members"], {"x": {"role": "r"}}],
        [["_meta", "stamps", "a"], 1],
    ]


def test_a_changed_meta_hides_seeds_and_receipts():
    assert json.loads(export_changes(NEW, {'["_meta"]'}))["changes"]["set"] == [[["_meta"], VIEW["_meta"]]]


def test_a_changed_root_has_no_item_changes():
    assert export_changes(NEW, {"[]", '["overview"]'}) is None


def test_changes_rebuild_the_new_export_from_the_old():
    old = json.loads(json.dumps(OLD))
    assert api.patched(old, json.loads(export_changes(NEW, PATHS))["changes"]) == VIEW


class Server:
    def __init__(self, *replies):
        self.replies, self.sent = list(replies), []

    def __call__(self, client, slug, path, payload=None):
        self.sent.append((client.base, slug, path, payload))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def refused(code):
    return urllib.error.HTTPError("u", code, "refused", {}, io.BytesIO(b"{}"))


def reader(monkeypatch, *replies, base="http://a"):
    server = Server(*replies)
    monkeypatch.setattr(api.ResourceClient, "request", lambda client, *args: server(client, *args))
    return api.ResourceClient(base, {}), server


def test_kept_export_hands_out_a_copy_and_asks_for_changes_next(monkeypatch):
    client, server = reader(
        monkeypatch,
        {"generation": 5, "data": {"chat": [1]}},
        {"generation": 6, "changes": {"lists": {}, "set": [[["chat"], [2]]], "drop": []}},
    )
    with api.kept_exports():
        first = client.export("s")
        first["chat"].append(9)
        assert client.export("s") == {"chat": [2]}
        assert api.KEPT[("http://a", "s")] == (6, {"chat": [2]})
    assert [payload for *_, payload in server.sent] == [{"since": 0}, {"since": 5}]


def test_kept_exports_are_per_server(monkeypatch):
    client, server = reader(monkeypatch, {"generation": 8, "data": {}}, base="http://b")
    with api.kept_exports():
        api.KEPT[("http://a", "s")] = (5, {})
        api.KEPT[("http://b", "t")] = (6, {})
        client.export("s")
        assert api.KEPT[("http://b", "s")] == (8, {})
    assert server.sent == [("http://b", "s", "export", {"since": 0})]


def test_a_server_without_changes_answers_the_plain_export(monkeypatch):
    client, server = reader(monkeypatch, refused(400), {"data": {"chat": []}})
    with api.kept_exports():
        api.KEPT[("http://a", "s")] = (5, {})
        assert client.export("s") == {"chat": []}
        assert api.KEPT == {}
    assert server.sent == [("http://a", "s", "export", {"since": 5}), ("http://a", "s", "export", {})]


def test_other_refusals_reach_the_caller(monkeypatch):
    client, _ = reader(monkeypatch, refused(403))
    with api.kept_exports(), pytest.raises(urllib.error.HTTPError):
        client.export("s")


def test_changes_that_do_not_fit_the_kept_export_read_it_whole(monkeypatch):
    broken = {"generation": 6, "changes": {"lists": {"tasks": {"order": ["x"], "items": {}}}, "set": [], "drop": []}}
    client, server = reader(monkeypatch, broken, {"generation": 7, "data": {"tasks": []}})
    with api.kept_exports():
        api.KEPT[("http://a", "s")] = (5, {"tasks": []})
        assert client.export("s") == {"tasks": []}
        assert api.KEPT[("http://a", "s")] == (7, {"tasks": []})
    assert [payload for *_, payload in server.sent] == [{"since": 5}, {"since": 0}]
    assert api.KEPT is None


def test_a_first_read_refused_or_unfit_needs_nothing_kept(monkeypatch):
    unfit = {"generation": 6, "changes": {"lists": {}, "set": [[["chat"], [1]]], "drop": []}}
    client, server = reader(monkeypatch, refused(400), {"data": {}}, unfit, {"generation": 7, "data": {"chat": []}})
    with api.kept_exports():
        assert client.export("s") == {}
        assert client.export("s") == {"chat": []}
    assert [payload for *_, payload in server.sent] == [{"since": 0}, {}, {"since": 0}, {"since": 0}]
