import json

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
    assert sent == [("export", {"since": 0, "parts": ["tasks", "phases"]})]
    assert set(reply["data"]) == {"tasks", "phases"}


@pytest.mark.feature("swarm-tick-delta-reads")
def test_the_tick_view_parts_are_accepted(live: dict) -> None:
    from scripts.tick import view

    whole = request(live, "POST", "export", {"since": 0})[1]
    reply = parted(live, 0, list(view.PARTS))
    assert reply == {
        "generation": whole["generation"],
        "data": {key: whole["data"][key] for key in view.PARTS if key in whole["data"]},
    }
