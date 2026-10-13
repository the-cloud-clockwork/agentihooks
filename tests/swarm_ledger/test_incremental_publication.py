import json

from scripts.swarm_ledger import ledger_server as server
from scripts.swarm_ledger.events import patch
from scripts.swarm_ledger.events.hub import Hub


def test_a_write_copies_only_changed_parts_and_keeps_published_state_isolated(monkeypatch):
    hub = Hub()
    monkeypatch.setattr(server, "HUB", hub)
    before = {
        "tasks": [{"id": "t1", "title": "unchanged task"}],
        "notes": [],
        "_meta": {"rev": 1},
    }
    hub.open("s", lambda: {"ledger": before})
    serialized = []
    dumps = json.dumps

    def record(value, *args, **kwargs):
        text = dumps(value, *args, **kwargs)
        serialized.append(text)
        return text

    monkeypatch.setattr(json, "dumps", record)
    state = {
        "tasks": [{"id": "t1", "title": "unchanged task"}],
        "notes": [{"id": "n1", "text": "new note"}],
        "_meta": {"rev": 2},
    }
    server.publish_ledger("s", state)
    events = hub.wait("s", 0, 0)
    assert len(events) == 1
    update = events[0][3]["patch"]
    assert "tasks" not in update["o"]
    assert patch.apply(before, update)["notes"] == [{"id": "n1", "text": "new note"}]
    state["notes"][0]["text"] = "caller mutation"
    state["tasks"][0]["title"] = "caller mutation"
    assert hub.resource("s", "ledger")["notes"] == [{"id": "n1", "text": "new note"}]
    assert hub.resource("s", "ledger")["tasks"] == [{"id": "t1", "title": "unchanged task"}]
    assert all("unchanged task" not in text for text in serialized)


def test_the_initial_snapshot_is_isolated_from_its_loader_before_the_first_write(monkeypatch):
    hub = Hub()
    monkeypatch.setattr(server, "HUB", hub)
    state = {"tasks": [{"id": "t1", "title": "first"}], "notes": [], "_meta": {"rev": 1}}
    hub.open("s", lambda: {"ledger": server.ledger_view(state)})
    state["tasks"][0]["title"] = "changed"
    state["_meta"]["rev"] = 2
    assert hub.resource("s", "ledger")["tasks"] == [{"id": "t1", "title": "first"}]
    server.publish_ledger("s", state)
    events = hub.wait("s", 0, 0)
    assert len(events) == 1
    assert events[0][3]["patch"]["o"]["tasks"]["u"] == [{"id": "t1", "title": "changed"}]


def test_a_resource_published_during_initial_loading_owns_its_nested_values(monkeypatch):
    hub = Hub()
    monkeypatch.setattr(server, "HUB", hub)
    state = {"tasks": [{"id": "t1", "title": "first"}], "_meta": {"rev": 1}}

    def load():
        server.publish_ledger("s", state)
        return {}

    _, first = hub.open("s", load)
    state["tasks"][0]["title"] = "changed"
    state["_meta"]["rev"] = 2
    assert first[0][3]["ledger"]["tasks"] == [{"id": "t1", "title": "first"}]
    server.publish_ledger("s", state)
    events = hub.wait("s", 0, 0)
    assert events[0][3]["patch"]["o"]["tasks"]["u"] == [{"id": "t1", "title": "changed"}]
