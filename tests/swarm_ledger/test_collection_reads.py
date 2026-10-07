import urllib.error
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from unittest.mock import Mock

import pytest

from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm_ledger.api import resources
from scripts.swarm_ledger.api.client import ResourceClient
from tests.swarm_ledger.test_api_v1 import authority_live as _authority_live
from tests.swarm_ledger.test_api_v1 import live as _live
from tests.swarm_ledger.test_ledger_authority import SLUG, core
from tests.swarm_ledger.test_v1_clients import Scripted, http_error

pytestmark = pytest.mark.unit
authority_live = _authority_live
live = _live


@pytest.mark.parametrize("path", ["tasks", "events", "chat"])
def test_collection_reads_one_snapshot_while_writes_land(live, monkeypatch, path):
    core.sync(
        SLUG,
        ops=[
            {
                "op": "task_add",
                "id": f"seed-{index}",
                "by": "swarm",
                "task": f"t{index}",
                "title": "Seed",
                "lane": "eng",
            }
            for index in range(156)
        ]
        + [{"op": "add", "id": f"chat-{index}", "thread": "chat", "text": "Seed"} for index in range(156)],
    )
    work, completed = Queue(), Queue()
    calls, snapshots = [], []
    request = ResourceClient.request

    def writer():
        while (index := work.get()) is not None:
            core.sync(
                SLUG,
                ops=[
                    {
                        "op": "task_add",
                        "id": f"write-{index}",
                        "by": "swarm",
                        "task": f"w{index}",
                        "title": "Write",
                        "lane": "eng",
                    },
                    {"op": "add", "id": f"message-{index}", "thread": "chat", "text": "Write"},
                ],
            )
            completed.put(index)

    def write_after(self, slug, resource, payload=None):
        calls.append((resource, payload))
        reply = request(self, slug, resource, payload)
        if resource == "export":
            snapshots.append(reply["data"])
        work.put(len(calls))
        assert completed.get(timeout=5) == len(calls)
        return reply

    monkeypatch.setattr(ResourceClient, "request", write_after)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(writer)
        try:
            rows = getattr(LedgerClient(service=True), path)(SLUG)
        finally:
            work.put(None)
            future.result(timeout=5)
    assert len(calls) == 3
    assert calls[0] == (f"{path}?limit=100", None)
    assert calls[1][0].startswith(f"{path}?limit=100&cursor=")
    assert calls[2] == ("export", {})
    snapshot = snapshots[0]
    expected = []
    query = {"limit": 100}
    while True:
        page = resources.read(snapshot, path, query)
        expected.extend(page["data"])
        if page["next_cursor"] is None:
            break
        query["cursor"] = page["next_cursor"]
    assert rows == expected
    assert len({row["revision"] for row in rows}) == len(rows)
    if path == "tasks":
        assert [row["id"] for row in rows] == [f"t{index}" for index in range(156)] + ["w1"]


@pytest.mark.parametrize(
    "path", ["tasks", "events", "chat", "members", "sources", "threads", "tasks/t1/comments", "swarm/agents"]
)
def test_conflict_discards_partial_pages_and_preserves_resource_projection(path):
    row = {"id": "t1", "comments": [{"id": "c1", "text": "One"}], "answers": [], "title": "Task"}
    state = {
        "tasks": [row],
        "chat": [{"id": "m1", "text": "Message"}],
        "sources": ["Source"],
        "_meta": {"events": [{"id": "e1", "text": "Event"}], "members": {"worker": {"role": "eng"}}},
    }
    swarm = path.startswith("swarm/")
    snapshot = {"agents": [row]} if swarm else state
    read = resources.swarm_read if swarm else resources.read
    expected = read(snapshot, path, {"limit": 100})["data"]
    conflict = http_error(409, b'{"error":{"code":"revision_conflict","message":"Changed"}}')
    client = Scripted({"data": [{"id": "stale"}], "next_cursor": "old:1"}, conflict, {"data": snapshot})
    assert client.collection("proof", path) == expected
    assert client.calls == [
        (f"{path}?limit=100", None),
        (f"{path}?limit=100&cursor=old%3A1", None),
        ("swarm/export" if swarm else "export", {}),
    ]
    assert client.slugs == {"proof"}


@pytest.mark.parametrize(
    "status,body",
    [
        (403, b'{"error":{"code":"forbidden"}}'),
        (409, b'{"error":{"code":"operation_conflict"}}'),
        (500, b'{"error":{"code":"revision_conflict"}}'),
        (409, b"Unavailable"),
    ],
)
def test_collection_preserves_other_http_errors(status, body):
    client = Scripted(http_error(status, body))
    with pytest.raises(urllib.error.HTTPError) as refused:
        client.collection("proof", "tasks")
    assert refused.value.code == status
    assert refused.value.read() == body
    assert client.calls == [("tasks?limit=100", None)]


def test_snapshot_failure_is_not_retried():
    body = b'{"error":{"code":"revision_conflict"}}'
    client = Scripted(http_error(409, body), http_error(409, body))
    with pytest.raises(urllib.error.HTTPError) as refused:
        client.collection("proof", "tasks")
    assert refused.value.code == 409
    assert refused.value.read() == body
    assert client.calls == [("tasks?limit=100", None), ("export", {})]


def test_unchanged_collection_keeps_bounded_pagination():
    client = Scripted({"data": ["One"], "next_cursor": "same:1"}, {"data": ["Two"], "next_cursor": None})
    assert client.collection("proof", "sources") == ["One", "Two"]
    assert client.calls == [("sources?limit=100", None), ("sources?limit=100&cursor=same%3A1", None)]


def test_collection_propagates_transport_failure():
    client = ResourceClient("http://ledger.test", {})
    error = urllib.error.URLError("Offline")
    client.request = Mock(side_effect=error)
    with pytest.raises(urllib.error.URLError) as refused:
        client.collection("proof", "tasks")
    assert refused.value is error
    client.request.assert_called_once_with("proof", "tasks?limit=100")
