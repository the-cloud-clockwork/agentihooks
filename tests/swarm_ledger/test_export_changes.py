import json

from tests.swarm_ledger import test_api_v1
from tests.swarm_ledger.test_api_v1 import SLUG, request, send

authority_live = test_api_v1.authority_live
live = test_api_v1.live


def client():
    from scripts.swarm_ledger.api.client import ResourceClient
    from tests.swarm_ledger.test_ledger_authority import ledger

    return ResourceClient(ledger.BASE, ledger.credentials(SLUG, service=True))


def full(live):
    status, reply = request(live, "POST", "export", {"since": 0})
    assert status == 200
    return reply


def test_changes_since_a_generation_rebuild_the_full_export(live):
    from scripts.swarm_ledger.api.client import patched

    before = full(live)
    assert before["data"] == request(live, "POST", "export", {})[1]["data"]
    writes = client()
    edit = {"op": "edit", "id": "seed-chat-two", "thread": "chat", "text": "Edited"}
    assert writes.mutate(SLUG, [edit])["rejected"] == []
    assert (
        writes.mutate(SLUG, [{"op": "add", "id": "export-chat", "thread": "chat", "text": "Later"}])["rejected"] == []
    )
    assert writes.mutate(SLUG, [{"op": "delete", "id": "seed-chat-one", "thread": "chat"}])["rejected"] == []
    assert writes.mutate(SLUG, [{"op": "join", "id": "export-join", "by": "export-reader"}])["rejected"] == []
    status, delta = request(live, "POST", "export", {"since": before["generation"]})
    after = full(live)
    assert status == 200
    assert "data" not in delta
    assert delta["generation"] == after["generation"] > before["generation"]
    assert patched(json.loads(json.dumps(before["data"])), delta["changes"]) == after["data"]
    assert len(json.dumps(delta)) < len(json.dumps(after))


def test_an_unknown_generation_answers_the_full_export(live):
    current = full(live)
    for since in (current["generation"] + 1, 1):
        status, reply = request(live, "POST", "export", {"since": since})
        assert status == 200
        assert reply == current


def test_the_current_generation_answers_no_changes(live):
    current = full(live)
    status, reply = request(live, "POST", "export", {"since": current["generation"]})
    assert status == 200
    assert reply == {
        "generation": current["generation"],
        "changes": {"set": [[["_meta", "events"], current["data"]["_meta"]["events"]]], "drop": [], "lists": {}},
    }


def test_a_changed_root_answers_the_full_export(live, monkeypatch):
    from scripts.swarm_ledger.repository import refresh

    current = full(live)
    monkeypatch.setattr(refresh, "changed_paths", lambda *args: {"[]"})
    assert request(live, "POST", "export", {"since": current["generation"]}) == (200, current)


def test_changes_adopt_only_the_ledger_they_read(live, monkeypatch):
    from scripts.swarm_ledger.repository import legacy

    adopted, adopt = [], legacy.adopt
    monkeypatch.setattr(legacy, "adopt", lambda repository, slug=None: adopted.append(slug) or adopt(repository, slug))
    assert request(live, "POST", "export", {"since": 1})[0] == 200
    assert SLUG in adopted
    assert None not in adopted


def test_since_must_be_a_lone_positive_integer(live):
    for body in ({"since": "1"}, {"since": -1}, {"since": 1, "extra": True}):
        status, data, _ = send(
            live,
            "POST",
            f"/api/v1/ledgers/{SLUG}/export",
            json.dumps(body).encode(),
            **{"X-Ledger-Token": live["admin"], "Content-Type": "application/json"},
        )
        assert status == 400, body


def test_kept_exports_read_changes_and_hand_out_copies(live, monkeypatch):
    from scripts.swarm_ledger.api import client as api

    reader, sent = client(), []
    request_once = api.ResourceClient.request

    def recorded(self, slug, path, payload=None):
        sent.append((path, payload))
        return request_once(self, slug, path, payload)

    monkeypatch.setattr(api.ResourceClient, "request", recorded)
    with api.kept_exports():
        first = reader.snapshot(SLUG)
        first["tasks"].append({"id": "local-only"})
        assert (
            client().mutate(SLUG, [{"op": "add", "id": "kept-chat", "thread": "chat", "text": "Kept"}])["rejected"]
            == []
        )
        second = reader.snapshot(SLUG)
    exports = [payload for path, payload in sent if path == "export"]
    assert exports[0] == {"since": 0}
    assert exports[-1]["since"] > 0
    assert {"id": "local-only"} not in second["tasks"]
    assert any(entry["id"] == "kept-chat" for entry in second["chat"])
    assert second == client().snapshot(SLUG)
    assert api.KEPT is None
