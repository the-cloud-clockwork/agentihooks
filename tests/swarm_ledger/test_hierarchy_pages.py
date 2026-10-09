import io
import json
import urllib.error
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from scripts.swarm import ledger_client, phases
from scripts.swarm_ledger import ledger
from scripts.swarm_ledger.api import client as api_client
from scripts.swarm_ledger.api import resources, routes
from scripts.swarm_ledger.api.errors import APIError
from scripts.swarm_ledger.repository import sqlite as store

SLUG = "hierarchy-pages"


def state(phase_count, per_phase, flip=None):
    tasks = [
        {"id": f"t{n}-{m}", "phase": f"p{n}", "state": "done" if n % 2 == 0 else "open"}
        for n in range(phase_count)
        for m in range(per_phase)
    ]
    if flip:
        tasks[0]["state"] = flip
    return {
        "phases": [{"id": f"p{n}", "title": f"Phase {n}", "done": n % 2 == 1} for n in range(phase_count)],
        "tasks": tasks,
        "_meta": {"rev": 1},
    }


def repository(folder, document):
    folder.mkdir()
    found = store.SQLiteLedgerRepository(folder / store.DATABASE)
    found.import_document(SLUG, document)
    return found


@pytest.fixture(scope="module")
def large(tmp_path_factory):
    return repository(tmp_path_factory.mktemp("large") / "ledger", state(40, 100))


class Served(api_client.ResourceClient):
    def __init__(self, *repos):
        super().__init__("http://ledger.test", {})
        self.repos, self.paths = repos, []

    def request(self, slug, path, payload=None):
        repo = self.repos[len(self.paths) % len(self.repos)]
        self.paths.append(path)
        try:
            return routes.ledger_read(
                SimpleNamespace(repository=repo), slug, urlsplit(path).path, routes.pagination(path)
            )
        except APIError as exc:
            body = json.dumps({"error": {"code": exc.code, "message": str(exc)}}).encode()
            raise urllib.error.HTTPError(path, exc.status, str(exc), {}, io.BytesIO(body)) from None


def test_a_hierarchy_above_the_reply_cap_returns_every_node_across_pages(large):
    whole = large.nodes(SLUG, "subtree")
    assert resources.reply_size({"data": whole}) > resources.MAX_REPLY
    served = Served(large)
    rows = served.collection(SLUG, "hierarchy")
    assert [(row["node"], row["depth"]) for row in rows] == [(row["node"], row["depth"]) for row in whole]
    assert len(served.paths) == len(whole) // 100 + 1
    assert {row["state"] for row in rows if row["kind"] == "task"} == {"done", "open"}


def test_a_page_of_the_hierarchy_carries_a_cursor_to_the_next(large):
    first = routes.ledger_read(SimpleNamespace(repository=large), SLUG, "hierarchy", {"limit": 3})
    assert [row["node"] for row in first["data"]] == ["phases/p0", "tasks/t0-0", "tasks/t0-1"]
    second = routes.ledger_read(
        SimpleNamespace(repository=large), SLUG, "hierarchy", {"limit": 2, "cursor": first["next_cursor"]}
    )
    assert [row["node"] for row in second["data"]] == ["tasks/t0-2", "tasks/t0-3"]
    assert second["revision"] == first["revision"]


def test_a_node_read_pages_its_subtree(large):
    rows = Served(large).collection(SLUG, "hierarchy/subtree/phases/p3")
    assert [row["node"] for row in rows] == ["phases/p3", *(f"tasks/t3-{m}" for m in range(100))]


@pytest.fixture
def pauses(monkeypatch):
    seen = []
    monkeypatch.setattr(api_client.time, "sleep", seen.append)
    return seen


def backoff(attempt, pause):
    return api_client.BACKOFF * 2**attempt / 2 <= pause <= api_client.BACKOFF * 2**attempt


def test_a_hierarchy_that_changes_between_pages_restarts_from_the_first_page(tmp_path, pauses):
    before = repository(tmp_path / "before", state(2, 80))
    after = repository(tmp_path / "after", state(2, 80, flip="claimed"))
    served = Served(before, after, after, after)
    rows = served.collection(SLUG, "hierarchy")
    assert ("tasks/t0-0", "claimed") in [(row["node"], row["state"]) for row in rows]
    assert len(rows) == 162
    assert [urlsplit(path).query.startswith("limit=100&cursor=") for path in served.paths] == [
        False,
        True,
        False,
        True,
    ]
    assert len(pauses) == 1 and backoff(0, pauses[0])


def test_a_hierarchy_that_keeps_changing_gives_up_with_the_conflict(tmp_path, pauses):
    served = Served(repository(tmp_path / "a", state(2, 80)), repository(tmp_path / "b", state(2, 80, "pr")))
    with pytest.raises(urllib.error.HTTPError) as caught:
        served.collection(SLUG, "hierarchy")
    assert caught.value.code == 409
    assert len(served.paths) == 2 * (api_client.RETRIES + 1)
    assert len(pauses) == api_client.RETRIES and all(backoff(n, pause) for n, pause in enumerate(pauses))


class Inbox:
    def __init__(self):
        self.sent = []

    def send(self, sender, to, text, fyi=False):
        self.sent.append(text)


def test_the_phase_pass_reads_every_page_of_a_ledger_above_the_cap(large, monkeypatch):
    served = Served(large)
    flat = ledger_client._ledger()
    monkeypatch.setattr(api_client, "ResourceClient", lambda base, credentials, timeout: served)
    monkeypatch.setattr(flat, "credentials", lambda slug, service=False: {})
    monkeypatch.setattr(flat, "base", lambda: "http://ledger.test")
    ticked = []

    class Ledger(ledger_client.LedgerClient):
        def set_phase(self, slug, phase_id, done, status):
            ticked.append((phase_id, done))

    document = state(40, 100)
    actions = phases.phase_pass(Inbox(), SimpleNamespace(agents=lambda slug: []), SLUG, document, Ledger())
    assert ticked == [(f"p{n}", n % 2 == 0) for n in range(40)]
    assert actions == [f"phase p{n} {'ticked' if n % 2 == 0 else 'reopened'}" for n in range(40)]
    assert len(served.paths) > 1


def test_tree_prints_every_page(large, monkeypatch, capsys):
    read = []

    def resource(slug, path, collection=False):
        read.append((path, collection))
        return Served(large).collection(slug, path)

    monkeypatch.setattr(ledger, "resource", resource)
    ledger.cmd_tree(SimpleNamespace(slug=SLUG, node=None))
    assert read == [("hierarchy", True)]
    assert len(capsys.readouterr().out.splitlines()) == 4040
