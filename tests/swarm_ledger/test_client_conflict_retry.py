import io
import json
import threading
import urllib.error

import pytest

from scripts.swarm_ledger.api import client as api_client
from tests.swarm_ledger.test_api_v1 import authority_live, live  # noqa: F401
from tests.swarm_ledger.test_ledger_authority import SLUG

pytestmark = pytest.mark.xdist_group("fakeredis")

CONFLICT = b'{"error": {"code": "revision_conflict", "message": "Resource changed since the expected revision"}}'


def conflict():
    return urllib.error.HTTPError("http://ledger.test", 409, "Conflict", {}, io.BytesIO(CONFLICT))


class Scripted(api_client.ResourceClient):
    def __init__(self, *replies):
        super().__init__("http://ledger.test", {"X-Ledger-Token": "t"})
        self.replies, self.calls = list(replies), []

    def request(self, slug, path, payload=None):
        self.calls.append((path, json.loads(json.dumps(payload))))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def pauses(monkeypatch):
    seen = []
    monkeypatch.setattr(api_client.time, "sleep", seen.append)
    return seen


def conflicting(times, final):
    replies = []
    for attempt in range(times):
        replies += [{"revision": f"r{attempt}"}, conflict()]
    return Scripted(*replies, {"revision": f"r{times}"}, final)


def chat_add():
    return [{"op": "add", "id": "m-1", "thread": "chat", "text": "Hi", "by": "swarm"}]


def test_a_server_conflicting_four_times_lands_the_write_on_the_fifth_send(pauses):
    client = conflicting(4, {"applied": ["m-1"]})
    assert client.mutate(SLUG, chat_add()) == {"applied": ["m-1"]}
    posts = [payload for path, payload in client.calls if path == "operations"]
    assert [post["guards"] for post in posts] == [{"chat": f"r{attempt}"} for attempt in range(5)]
    assert len({post["operation_id"] for post in posts}) == 1
    assert len(pauses) == 4
    assert all(0 < pause <= api_client.BACKOFF * 2**attempt for attempt, pause in enumerate(pauses))


def test_backoff_is_jittered(pauses, monkeypatch):
    monkeypatch.setattr(api_client.random, "uniform", lambda low, high: (low, high))
    conflicting(2, {"applied": ["m-1"]}).mutate(SLUG, chat_add())
    assert pauses == [(api_client.BACKOFF / 2, api_client.BACKOFF), (api_client.BACKOFF, api_client.BACKOFF * 2)]


def test_a_conflict_past_five_retries_fails_naming_the_conflict(pauses):
    client = conflicting(6, None)
    with pytest.raises(urllib.error.HTTPError) as error:
        client.mutate(SLUG, chat_add())
    assert error.value.code == 409
    body = json.loads(error.value.read())["error"]
    assert body["code"] == "revision_conflict"
    assert body["message"] == f"Resource changed since the expected revision: {error.value.msg}"
    assert error.value.msg == "revision conflict persisted after 5 retries on a batch guarding chat"
    assert [path for path, _ in client.calls].count("operations") == 6
    assert len(pauses) == 5


def test_a_conflict_without_a_server_message_names_only_the_retries(pauses):
    body = b'{"error": {"code": "revision_conflict"}}'
    replies = [
        reply
        for n in range(6)
        for reply in ({"revision": f"r{n}"}, urllib.error.HTTPError("u", 409, "", {}, io.BytesIO(body)))
    ]
    with pytest.raises(urllib.error.HTTPError) as error:
        Scripted(*replies).mutate(SLUG, chat_add())
    assert json.loads(error.value.read())["error"]["message"] == error.value.msg


def test_a_pinned_operation_keeps_its_revision_across_retries(pauses):
    pinned = {"op": "add", "id": "m-2", "thread": "chat", "text": "Pinned", "by": "swarm", "expected_revision": "p0"}
    client = Scripted({"revision": "r0"}, conflict(), {"revision": "r1"}, {"applied": ["m-1", "m-2"]})
    operations = [*chat_add(), pinned]
    client.mutate(SLUG, operations)
    assert operations[1]["expected_revision"] == "p0"
    assert [path for path, _ in client.calls] == ["chat", "operations", "chat", "operations"]


def test_a_retry_reuses_the_operation_id_so_an_applied_write_is_not_duplicated(live):  # noqa: F811
    from tests.swarm_ledger.test_ledger_authority import ledger

    client = api_client.ResourceClient(ledger.BASE, ledger.credentials(SLUG, service=True))
    operations = [{"op": "add", "id": "retry-once", "thread": "chat", "text": "Once"}]
    first = client.mutate(SLUG, operations)
    client.mutate(SLUG, [{"op": "add", "id": "other-write", "thread": "chat", "text": "Moves the revision"}])
    retry = [{key: value for key, value in operations[0].items() if key != "expected_revision"}]
    replay = client.mutate(SLUG, retry)
    assert (replay["applied"], replay["rejected"]) == (first["applied"], first["rejected"])
    assert [row["id"] for row in client.collection(SLUG, "chat")].count("retry-once") == 1


TITLES = [
    "Apple river stone",
    "Maple tiger orange",
    "Violet harbor candle",
    "Meadow falcon copper",
    "Silver garden willow",
    "Lantern compass thunder",
]


class Crowded(api_client.ResourceClient):
    def __init__(self, base, credentials, barrier, conflicts):
        super().__init__(base, credentials)
        self.barrier, self.conflicts = barrier, conflicts

    def request(self, slug, path, payload=None):
        if path == "tasks" and self.barrier is not None:
            reply = super().request(slug, path, payload)
            self.barrier, barrier = None, self.barrier
            barrier.wait()
            return reply
        try:
            return super().request(slug, path, payload)
        except urllib.error.HTTPError as exc:
            self.conflicts.append(exc.code)
            raise


def test_a_burst_of_writers_reading_one_revision_all_land(live):  # noqa: F811
    from tests.swarm_ledger.test_ledger_authority import ledger

    writers, conflicts, results = api_client.RETRIES + 1, [], {}
    barrier = threading.Barrier(writers, timeout=30)

    def add(n):
        client = Crowded(ledger.BASE, ledger.credentials(SLUG, service=True), barrier, conflicts)
        operation = {"op": "task_add", "id": f"burst-{n}", "by": "swarm", "task": f"b{n}", "lane": "eng"}
        results[n] = client.mutate(SLUG, [{**operation, "title": TITLES[n]}])["applied"]

    threads = [threading.Thread(target=add, args=(n,)) for n in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == {n: [f"burst-{n}"] for n in range(writers)}
    assert conflicts.count(409) >= writers - 1
    client = api_client.ResourceClient(ledger.BASE, ledger.credentials(SLUG, service=True))
    assert {f"b{n}" for n in range(writers)} <= {row["id"] for row in client.collection(SLUG, "tasks")}
