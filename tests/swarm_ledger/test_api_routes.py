import re
from types import SimpleNamespace

import pytest

from scripts.swarm_ledger.api import resources, routes
from scripts.swarm_ledger.api.errors import APIError
from scripts.swarm_ledger.events import Expired, Hub, stream

SLUG = "routes-2026-01-01"


def fake_server(calls):
    return SimpleNamespace(
        ALLOWED_HOSTS={"h"},
        ALLOWED_ORIGINS=set(),
        HUB=Hub(),
        core=SimpleNamespace(SLUG_RE=re.compile(r"[a-z0-9-]+"), read_token=lambda page: "tok"),
        authority=SimpleNamespace(principal=lambda token, slug, given, agent: "operator"),
        repository=SimpleNamespace(token=lambda slug: "tok", get_document=lambda slug: {"events": []}),
        stream_resources=lambda slug: calls.append(("load", slug)) or {"ledger": {"_meta": {"rev": 1}}},
    )


def handler(accept=None):
    headers = {"Host": "h"} if accept is None else {"Host": "h", "Accept": accept}
    return SimpleNamespace(
        headers=headers, path=f"/api/v1/ledgers/{SLUG}/events", command="GET", exists=lambda slug: True
    )


def test_the_events_stream_serves_the_request_with_the_server_hub_and_loader(monkeypatch):
    calls = []
    server = fake_server(calls)
    request = handler()

    def serve(given, hub, slug, load):
        calls.append((given, hub, slug, load()))

    monkeypatch.setattr(stream, "serve", serve)
    assert routes.events(request, server, SLUG) is None
    assert calls == [("load", SLUG), (request, server.HUB, SLUG, {"ledger": {"_meta": {"rev": 1}}})]


def test_an_expired_cursor_answers_410_with_a_reload_hint(monkeypatch):
    def serve(*_):
        raise Expired

    monkeypatch.setattr(stream, "serve", serve)
    with pytest.raises(APIError) as caught:
        routes.events(handler(), fake_server([]), SLUG)
    assert (caught.value.status, caught.value.envelope()) == (
        410,
        {"error": {"code": "cursor_expired", "message": "Cursor no longer retained; reconnect without it to reload"}},
    )


def test_only_a_stream_accept_header_turns_the_events_path_into_a_stream(monkeypatch):
    seen = []
    monkeypatch.setattr(routes, "events", lambda request, server, slug: seen.append(("stream", slug)))
    monkeypatch.setattr(resources, "read", lambda state, path, query: seen.append(("collection", path)) or {"data": []})
    server = fake_server([])
    assert routes.dispatch(handler("text/event-stream"), server) is None
    assert routes.dispatch(handler(), server) == {"data": []}
    assert routes.dispatch(handler("application/json"), server) == {"data": []}
    assert seen == [("stream", SLUG), ("collection", "events"), ("collection", "events")]


def test_a_task_work_folder_read_answers_its_lines_with_their_revision():
    server = SimpleNamespace(workspace_tails=lambda slug, task: {"latest_progress": f"{slug} {task}"})
    data = {"latest_progress": f"{SLUG} t1"}
    assert routes.workspace(server, SLUG, "t1") == {"data": data, "revision": resources.revision(data)}


def test_an_unsafe_task_work_folder_answers_not_found():
    def refuse(slug, task):
        raise ValueError(task)

    with pytest.raises(APIError) as caught:
        routes.workspace(SimpleNamespace(workspace_tails=refuse), SLUG, "t 1")
    assert (caught.value.status, caught.value.envelope()) == (
        404,
        {"error": {"code": "resource_missing", "message": "No such task work folder"}},
    )


@pytest.mark.parametrize(
    ("path", "routed"), [("tasks/t1/workspace", True), ("tasks/t1/workspaces", False), ("x/tasks/t1/workspace", False)]
)
def test_only_the_exact_task_work_folder_path_reads_the_folder(path, routed):
    assert bool(routes.WORKSPACE_RE.fullmatch(path)) is routed
