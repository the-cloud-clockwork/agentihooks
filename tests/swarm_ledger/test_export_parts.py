import json
from pathlib import Path
from typing import Any

import pytest

from tests.swarm_ledger import test_api_v1
from tests.swarm_ledger.test_api_v1 import SLUG, request, send
from tests.swarm_ledger.test_export_changes import client

authority_live = test_api_v1.authority_live
live = test_api_v1.live

PARTS = ["tasks", "_meta", "phases"]


def parted(live: dict, since: int, parts: list) -> dict:
    status, reply = request(live, "POST", "export", {"since": since, "parts": parts})
    assert status == 200
    return reply


@pytest.mark.feature("swarm-tick-delta-reads")
def test_parts_limit_the_full_export_to_the_named_parts(live: dict) -> None:
    whole = request(live, "POST", "export", {"since": 0})[1]
    reply = parted(live, 0, PARTS)
    assert reply == {"generation": whole["generation"], "data": {key: whole["data"][key] for key in PARTS}}


@pytest.mark.feature("swarm-tick-delta-reads")
def test_parts_limit_the_changes_to_the_named_parts(live: dict) -> None:
    from scripts.swarm_ledger.api.client import patched

    before = parted(live, 0, PARTS)
    writes = client()
    assert (
        writes.mutate(SLUG, [{"op": "add", "id": "parts-chat", "thread": "chat", "text": "Elsewhere"}])["rejected"]
        == []
    )
    assert writes.mutate(SLUG, [{"op": "join", "id": "parts-join", "by": "parts-reader"}])["rejected"] == []
    delta = parted(live, before["generation"], PARTS)
    after = parted(live, 0, PARTS)
    assert "data" not in delta
    assert delta["generation"] == after["generation"] > before["generation"]
    assert delta["changes"]["lists"] == {}
    assert all(path[0] in PARTS for path, _ in delta["changes"]["set"])
    assert patched(json.loads(json.dumps(before["data"])), delta["changes"]) == after["data"]


@pytest.mark.feature("swarm-tick-delta-reads")
def test_parts_without_meta_carry_no_events(live: dict) -> None:
    current = parted(live, 0, ["tasks"])
    assert parted(live, current["generation"], ["tasks"]) == {
        "generation": current["generation"],
        "changes": {"set": [], "drop": [], "lists": {}},
    }


@pytest.mark.feature("swarm-tick-delta-reads")
def test_a_changed_root_answers_the_named_parts_in_full(live: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm_ledger.repository import refresh

    current = parted(live, 0, PARTS)
    monkeypatch.setattr(refresh, "changed_paths", lambda *args: {"[]"})
    assert parted(live, current["generation"], PARTS) == current


@pytest.mark.feature("swarm-tick-delta-reads")
def test_each_part_set_keeps_its_own_full_export(live: dict) -> None:
    assert set(parted(live, 0, ["tasks"])["data"]) == {"tasks"}
    assert set(parted(live, 0, ["phases"])["data"]) == {"phases"}
    assert set(parted(live, 0, PARTS)["data"]) == set(PARTS)


@pytest.mark.feature("swarm-tick-delta-reads")
def test_parts_must_be_a_list_of_distinct_names(live: dict) -> None:
    for body in (
        {"since": 0, "parts": "tasks"},
        {"since": 0, "parts": []},
        {"since": 0, "parts": ["tasks", "tasks"]},
        {"since": 0, "parts": [1]},
        {"since": 0, "parts": ["chat"]},
        {"since": 0, "parts": ["tasks", "comments"]},
        {"parts": ["tasks"]},
    ):
        status, _, _ = send(
            live,
            "POST",
            f"/api/v1/ledgers/{SLUG}/export",
            json.dumps(body).encode(),
            **{"X-Ledger-Token": live["admin"], "Content-Type": "application/json"},
        )
        assert status == 400, body


@pytest.mark.feature("swarm-tick-delta-reads")
def test_the_client_reads_changes_of_the_named_parts(live: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm_ledger.api import client as api

    reader, sent = client(), []
    request_once = api.ResourceClient.request

    def recorded(self: api.ResourceClient, slug: str, path: str, payload: dict | None = None) -> dict:
        sent.append((path, payload))
        return request_once(self, slug, path, payload)

    monkeypatch.setattr(api.ResourceClient, "request", recorded)
    reply = reader.changes(SLUG, 0, ("tasks", "phases"))
    assert sent == [("export", {"since": 0, "parts": ["tasks", "phases"], "closed": False, "frozen": False})]
    assert set(reply["data"]) == {"tasks", "phases", "frozen_ids"}


@pytest.mark.feature("swarm-tick-delta-reads")
def test_the_tick_view_parts_are_accepted(live: dict) -> None:
    from scripts.tick import view

    whole = request(live, "POST", "export", {"since": 0})[1]
    reply = parted(live, 0, list(view.PARTS))
    assert reply == {
        "generation": whole["generation"],
        "data": {key: whole["data"][key] for key in view.PARTS if key in whole["data"]},
    }


@pytest.mark.feature("tick-view-plan-graph")
def test_the_held_tick_view_admits_tasks_under_focused_slices_and_plans(tmp_path: Path) -> None:
    from scripts.swarm import freeze
    from scripts.swarm_ledger.api.routes import export_text
    from scripts.swarm_ledger.repository import fragments, hierarchy
    from scripts.tick import view

    document: dict[str, Any] = {"_meta": {}}
    document.update(
        plans=[{"id": "plan-one"}, {"id": "plan-two"}],
        phases=[
            {"id": "phase-one", "plan": "plans/plan-one"},
            {"id": "phase-two", "plan": "plans/plan-two"},
            {"id": "phase-other"},
        ],
        slices=[
            {"id": "slice-one", "phase": "phases/phase-one"},
            {"id": "slice-two", "phase": "phases/phase-two"},
        ],
        tasks=[
            {"id": "sliced", "slice": "slices/slice-one", "phase": "phase-one", "state": "open"},
            {"id": "direct", "phase": "phase-one", "state": "open"},
            {"id": "planned", "slice": "slices/slice-two", "phase": "phase-two", "state": "open"},
            {"id": "other", "phase": "phase-other", "state": "open"},
        ],
        freezes=[
            {"verb": "focus", "target": "phases/phase-one"},
            {"verb": "focus", "target": "plans/plan-two"},
        ],
    )

    def reader(since: int, parts: tuple[str, ...], after: int | None) -> dict[str, Any]:
        assert since == 0
        return {"generation": 7, **json.loads(export_text(fragments.document(document), parts))}

    held = view.refreshed(tmp_path / "view.json", reader)
    expected = frozenset({"sliced", "direct", "planned"})
    assert freeze.admitted(document, hierarchy.project(document)[0]) == expected
    assert freeze.admitted(held.document, hierarchy.project(held.document)[0]) == expected
    assert held.document["slices"] == document["slices"]
    assert held.document["plans"] == document["plans"]


@pytest.mark.feature("tick-view-plan-graph")
def test_a_changed_tick_part_contract_replaces_the_old_cache_at_the_same_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm_ledger.api.client import read_export_file, write_export_file
    from scripts.tick import view

    old_parts = ("tasks", "_meta", "phases", "freezes", "priorities", "followups", "alerts", "overview")
    monkeypatch.setattr(view, "PARTS", old_parts)
    old_path = view.view_file("https://ledger.example", "proof", tmp_path)
    write_export_file(old_path, 7, {"tasks": [], "_meta": {}})
    new_parts = (*old_parts, "slices", "plans")
    monkeypatch.setattr(view, "PARTS", new_parts)
    new_path = view.view_file("https://ledger.example", "proof", tmp_path)
    assert new_path != old_path
    complete = {"tasks": [], "_meta": {}, "slices": [{"id": "slice-one"}], "plans": [{"id": "plan-one"}]}
    calls: list[tuple[int, tuple[str, ...], int | None]] = []

    def reader(since: int, parts: tuple[str, ...], after: int | None) -> dict[str, Any]:
        calls.append((since, parts, after))
        if since == 0:
            return {"generation": 7, "data": complete}
        return {"generation": 7, "changes": {"set": [], "drop": [], "lists": {}}}

    assert view.refreshed(new_path, reader).document == complete
    assert view.refreshed(new_path, reader).document == complete
    assert calls == [(0, new_parts, None), (7, new_parts, None)]
    assert read_export_file(new_path) == (7, complete)
    assert read_export_file(old_path) == (7, {"tasks": [], "_meta": {}})


@pytest.mark.feature("tick-view-plan-graph")
def test_the_held_tick_view_patches_slice_and_plan_rows_in_order(tmp_path: Path) -> None:
    from scripts.swarm_ledger.api.client import read_export_file
    from scripts.swarm_ledger.api.routes import export_changes
    from scripts.tick import view

    before = {
        "_meta": {"events": []},
        "tasks": [],
        "slices": [{"id": "slice-one", "phase": "phases/old"}, {"id": "slice-kept"}],
        "plans": [{"id": "plan-one", "title": "Old"}, {"id": "plan-kept"}],
    }
    after = {
        "_meta": {"events": []},
        "tasks": [],
        "slices": [{"id": "slice-kept"}, {"id": "slice-one", "phase": "phases/new"}],
        "plans": [{"id": "plan-kept"}, {"id": "plan-one", "title": "New"}, {"id": "plan-added"}],
    }
    paths = {
        json.dumps(["slices", '["id","slice-one",0]', "phase"]),
        json.dumps(["plans", '["id","plan-one",0]', "title"]),
        json.dumps(["plans", '["id","plan-added",0]']),
    }
    calls: list[int] = []

    def reader(since: int, parts: tuple[str, ...], revision: int | None) -> dict[str, Any]:
        calls.append(since)
        if since == 0:
            return {"generation": 6, "data": {key: value for key, value in before.items() if key in parts}}
        changes = export_changes(after, paths, parts)
        assert changes is not None
        return {"generation": 7, **json.loads(changes)}

    path = tmp_path / "view.json"
    view.refreshed(path, reader)
    held = view.refreshed(path, reader)
    assert calls == [0, 6]
    assert held.generation == 7
    assert held.document["slices"] == after["slices"]
    assert held.document["plans"] == after["plans"]
    assert held.document["tasks"] == []
    assert held.document["_meta"] == {"events": []}
    assert read_export_file(path) == (7, held.document)
