"""Scoped exports reuse cached task bytes, and change replies carry only the events newer than the reader holds."""

import json
import types
import urllib.error
from email.message import Message
from pathlib import Path

import pytest

from scripts.swarm_ledger.api import client as api
from scripts.swarm_ledger.api import routes
from scripts.swarm_ledger.api.client import write_export_file
from scripts.swarm_ledger.repository import live_rows
from scripts.swarm_ledger.repository.fragments import document
from scripts.tick import view
from tests.swarm_ledger.test_sqlite import CONTENT, chat, store

SCOPE = live_rows.Scope(closed=True, frozen=True)
EVENTS = [{"rev": 1, "e": "a"}, {"rev": 2, "e": "b"}, {"rev": 2, "e": "c"}, {"rev": 3, "e": "d"}, {"rev": 4, "e": "f"}]
STATE = {
    "title": "café",
    "tasks": [
        {"id": "a", "state": "open", "note": "🟣"},
        {"id": "b", "state": "done"},
        {"id": "c", "state": "open", "frozen": True, "focus_rank": 2},
    ],
    "_meta": {"rev": 4, "events": EVENTS, "seeds": [1]},
}


def scoped_view(state: dict, answer: live_rows.Answer) -> bytes:
    """The scoped export encoded from the parsed state alone."""
    meta = {key: value for key, value in state["_meta"].items() if key not in routes.HIDDEN_META}
    data = {**state, "tasks": answer.rows, "_meta": meta, live_rows.FROZEN_IDS: answer.frozen_ids}
    return json.dumps({"data": data}, ensure_ascii=False, separators=(",", ":")).encode()


@pytest.mark.feature("ledger-export-cost")
def test_a_scoped_export_builds_each_task_row_from_the_cached_bytes_of_its_task() -> None:
    fragments = document(STATE)
    parts = fragments.parts
    assert isinstance(parts, dict)
    tasks = parts["tasks"].parts
    assert isinstance(tasks, tuple)
    object.__setattr__(tasks[0], "data", b'{"id":"a","planted":1}')
    answer = live_rows.answer(STATE, SCOPE)
    rows = json.loads(routes.export_text(fragments, (), answer))["data"]["tasks"]
    assert rows[0] == {"id": "a", "planted": 1, "frozen": False, "focus_rank": None}


@pytest.mark.feature("ledger-export-cost")
@pytest.mark.parametrize("scope", [SCOPE, live_rows.Scope()])
def test_a_scoped_export_equals_the_encoded_scoped_view(scope: live_rows.Scope) -> None:
    fragments = document(STATE)
    routes.export_text(fragments)
    answer = live_rows.answer(STATE, scope)
    assert routes.export_text(fragments, (), answer) == scoped_view(STATE, answer)


@pytest.mark.feature("ledger-export-cost")
def test_a_change_reply_names_only_the_events_newer_than_the_reader_holds() -> None:
    reply = json.loads(routes.export_changes(STATE, {'["_meta","rev"]'}, events_after=2) or "")
    assert reply["changes"]["set"] == [[["_meta", "rev"], 4]]
    assert reply["changes"]["events"] == {"after": 2, "tail": EVENTS[3:], "count": 5}


@pytest.mark.feature("ledger-export-cost")
def test_a_held_revision_the_server_no_longer_holds_gets_the_whole_events_list() -> None:
    events = [EVENTS[0], EVENTS[1], EVENTS[4]]
    state = {**STATE, "_meta": {"rev": 4, "events": events}}
    reply = json.loads(routes.export_changes(state, {'["_meta","rev"]'}, events_after=3) or "")
    assert reply["changes"]["set"] == [[["_meta", "rev"], 4], [["_meta", "events"], events]]
    assert "events" not in reply["changes"]


@pytest.mark.feature("ledger-export-cost")
def test_a_reader_behind_every_held_event_gets_them_all_as_the_tail() -> None:
    reply = json.loads(routes.export_changes(STATE, {'["_meta","rev"]'}, events_after=0) or "")
    assert reply["changes"]["events"] == {"after": 0, "tail": EVENTS, "count": 5}


@pytest.mark.feature("ledger-export-cost")
def test_a_change_reply_without_a_held_revision_still_sets_the_whole_events_list() -> None:
    reply = json.loads(routes.export_changes(STATE, {'["_meta","rev"]'}) or "")
    assert [["_meta", "events"], EVENTS] in reply["changes"]["set"]
    assert "events" not in reply["changes"]


@pytest.mark.feature("ledger-export-cost")
def test_a_change_reply_for_parts_without_meta_names_no_events() -> None:
    reply = json.loads(routes.export_changes(STATE, {'["title"]'}, ("title",), events_after=2) or "")
    assert reply["changes"] == {"set": [[["title"], "café"]], "drop": [], "lists": {}}


@pytest.mark.feature("ledger-export-cost")
def test_patching_appends_the_newer_events_and_drops_the_oldest_past_the_count() -> None:
    held = {"_meta": {"rev": 2, "events": EVENTS[:3]}}
    changes = {"set": [], "drop": [], "lists": {}, "events": {"after": 2, "tail": EVENTS[3:], "count": 4}}
    assert api.patched(held, changes)["_meta"]["events"] == EVENTS[1:]


@pytest.mark.feature("ledger-export-cost")
def test_patching_keeps_no_event_when_the_count_is_zero() -> None:
    held = {"_meta": {"rev": 2, "events": EVENTS[:3]}}
    changes = {"set": [], "drop": [], "lists": {}, "events": {"after": 2, "tail": [], "count": 0}}
    assert api.patched(held, changes)["_meta"]["events"] == []


@pytest.mark.feature("ledger-export-cost")
def test_patching_refuses_events_that_do_not_reach_the_count() -> None:
    held = {"_meta": {"rev": 2, "events": EVENTS[2:3]}}
    changes = {"set": [], "drop": [], "lists": {}, "events": {"after": 2, "tail": EVENTS[3:], "count": 5}}
    with pytest.raises(KeyError):
        api.patched(held, changes)


@pytest.mark.feature("ledger-export-cost")
def test_the_last_held_revision_is_that_of_the_newest_held_event() -> None:
    assert api.last_revision({"_meta": {"events": EVENTS}}) == 4
    assert api.last_revision({"_meta": {"events": []}}) is None
    assert api.last_revision({"_meta": {}}) is None
    assert api.last_revision(None) is None


@pytest.mark.feature("ledger-export-cost")
def test_the_client_export_sends_the_newest_held_revision_and_merges_the_newer_events() -> None:
    sent: list[tuple[str, dict | None]] = []
    changes = {"set": [], "drop": [], "lists": {}, "events": {"after": 4, "tail": [{"rev": 5}], "count": 5}}

    def transport(slug: str, path: str, payload: dict | None) -> dict:
        sent.append((path, payload))
        return {"generation": 8, "changes": changes}

    kept = {("http://ledger", "s"): (7, {"_meta": {"events": EVENTS}})}
    state = api.ResourceClient("http://ledger", {}, transport=transport).export("s", kept)
    assert sent == [("export", {"since": 7, "events_after": 4})]
    assert state["_meta"]["events"] == [*EVENTS[1:], {"rev": 5}]


@pytest.mark.feature("ledger-export-cost")
def test_the_client_changes_read_carries_the_held_revision_when_there_is_one() -> None:
    sent: list[dict | None] = []

    def transport(slug: str, path: str, payload: dict | None) -> dict:
        sent.append(payload)
        return {}

    reader = api.ResourceClient("http://ledger", {}, transport=transport)
    reader.changes("s", 3, ("tasks",), SCOPE, 9)
    reader.changes("s", 3, ("tasks",), SCOPE)
    common = {"since": 3, "parts": ["tasks"], "closed": True, "frozen": True}
    assert sent == [{**common, "events_after": 9}, common]


@pytest.mark.feature("ledger-export-cost")
def test_a_server_that_refuses_the_held_revision_is_read_again_without_it() -> None:
    sent: list[dict | None] = []

    def transport(slug: str, path: str, payload: dict | None) -> dict:
        sent.append(payload)
        if payload and "events_after" in payload:
            raise urllib.error.HTTPError("http://ledger", 400, "schema_invalid", Message(), None)
        return {"generation": 4}

    reply = api.ResourceClient("http://ledger", {}, transport=transport).changes("s", 3, ("tasks",), SCOPE, 9)
    common = {"since": 3, "parts": ["tasks"], "closed": True, "frozen": True}
    assert (reply, sent) == ({"generation": 4}, [{**common, "events_after": 9}, common])


@pytest.mark.feature("ledger-export-cost")
@pytest.mark.parametrize(("code", "after"), [(500, 9), (400, None)])
def test_a_refusal_the_held_revision_did_not_cause_is_raised(code: int, after: int | None) -> None:
    def transport(slug: str, path: str, payload: dict | None) -> dict:
        raise urllib.error.HTTPError("http://ledger", code, "refused", Message(), None)

    with pytest.raises(urllib.error.HTTPError):
        api.ResourceClient("http://ledger", {}, transport=transport).changes("s", 3, ("tasks",), SCOPE, after)


@pytest.mark.feature("ledger-export-cost")
def test_the_tick_view_reads_changes_after_its_newest_held_event(tmp_path: Path) -> None:
    path = tmp_path / "view.json"
    write_export_file(path, 6, {"tasks": [], "_meta": {"rev": 3, "events": EVENTS}})
    calls: list[tuple[int, tuple[str, ...], int | None]] = []

    def reader(since: int, parts: tuple[str, ...], after: int | None = None) -> dict:
        calls.append((since, parts, after))
        return {"generation": 7, "changes": {"set": [], "drop": [], "lists": {}}}

    view.refreshed(path, reader)
    assert calls == [(6, view.PARTS, 4)]


@pytest.mark.feature("ledger-export-cost")
def test_the_export_route_answers_a_held_revision_with_only_the_newer_events(tmp_path: Path) -> None:
    repo = store(tmp_path)
    repo.create("ledger", CONTENT)
    repo.apply_ops("ledger", ops=[chat(1)])
    held = repo.current("ledger")
    events = held.state["_meta"]["events"]
    repo.apply_ops("ledger", ops=[chat(2)])
    handler = types.SimpleNamespace(send_export=lambda data: data)
    services = types.SimpleNamespace(repository=repo)
    payload = {"since": held.generation, "events_after": events[-1]["rev"]}
    reply = json.loads(routes.export(handler, services, "ledger", "export", payload))  # pyright: ignore[reportArgumentType]
    assert all(path != ["_meta", "events"] for path, _ in reply["changes"]["set"])
    newest = repo.current("ledger").state["_meta"]["events"]
    assert reply["changes"]["events"]["tail"] == [event for event in newest if event["rev"] > events[-1]["rev"]]
    assert reply["changes"]["events"]["count"] == len(newest)
