from types import SimpleNamespace

import pytest

from scripts.swarm_ledger import ledger_server as server
from scripts.swarm_ledger.api.routes import ledger_read
from scripts.swarm_ledger.events.hub import Hub
from scripts.swarm_ledger.events.publishing import Publishing
from tests.swarm_ledger.test_sqlite import CONTENT, chat
from tests.swarm_ledger.test_sqlite import store as stored

SLUG = "reads"
READS = ("tasks", "tasks/t1", "tasks/t1/comments", "metadata", "counts")


@pytest.fixture
def served(tmp_path, monkeypatch):
    store = stored(tmp_path)
    store.create(SLUG, CONTENT)
    hub, views = Hub(), []
    view = server.ledger_view
    monkeypatch.setattr(server, "HUB", hub)
    monkeypatch.setattr(server, "ledger_view", lambda state, *_: (views.append(state["_meta"]["rev"]), view(state))[1])
    hub.open(SLUG, lambda: {"ledger": view(store.get_document(SLUG))})
    return store, hub, views, SimpleNamespace(repository=Publishing(store, server.publish_ledger))


def test_reads_of_an_unchanged_ledger_publish_nothing(served):
    store, hub, views, host = served
    ledger_read(host, SLUG, "tasks", {})
    host.repository.drain(5)
    published = len(views)
    for path in READS * 3:
        ledger_read(host, SLUG, path, {})
    host.repository.drain(5)
    assert len(views) == published <= 1


def test_a_change_made_outside_the_server_reaches_open_streams_at_the_next_read_once(served):
    store, hub, views, host = served
    ledger_read(host, SLUG, "tasks", {})
    host.repository.drain(5)
    before, seq = len(views), hub.channels[SLUG].seq
    store.apply_ops(SLUG, ops=[chat(1)])
    for path in READS:
        ledger_read(host, SLUG, path, {})
    host.repository.drain(5)
    assert len(views) == before + 1
    assert hub.channels[SLUG].seq == seq + 1
    assert hub.resource(SLUG, "ledger")["chat"][-1]["text"] == "message 1"


def test_reads_never_decode_a_copy_of_the_whole_ledger(served, monkeypatch):
    store, _, _, host = served
    expected = {path: ledger_read(SimpleNamespace(repository=store), SLUG, path, {}) for path in READS}

    def decoded(slug):
        raise AssertionError("a collection read decoded the whole ledger")

    monkeypatch.setattr(store, "get_document", decoded)
    assert {path: ledger_read(host, SLUG, path, {}) for path in READS} == expected
    assert [row["id"] for row in ledger_read(host, SLUG, "tasks", {})["data"]] == ["t1", "t2"]
