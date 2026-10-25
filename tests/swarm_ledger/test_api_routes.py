import re
from email.message import Message
from types import SimpleNamespace

import pytest

from scripts.swarm_ledger.api import resources, routes
from scripts.swarm_ledger.api.errors import APIError
from scripts.swarm_ledger.events import Expired, Hub, stream
from tests.swarm_ledger.api_fakes import as_services

SLUG = "routes-2026-01-01"


def fake_server(calls):
    return SimpleNamespace(
        ALLOWED_HOSTS={"h"},
        ALLOWED_ORIGINS=set(),
        HUB=Hub(),
        core=SimpleNamespace(SLUG_RE=re.compile(r"[a-z0-9-]+"), read_token=lambda page: "tok"),
        authority=SimpleNamespace(caller=lambda headers, admin, slug: "operator"),
        repository=SimpleNamespace(
            token=lambda slug: "tok", current=lambda slug: SimpleNamespace(state={"events": []}, views={})
        ),
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
    assert routes.events(request, as_services(server), SLUG) is None
    assert calls == [("load", SLUG), (request, server.HUB, SLUG, {"ledger": {"_meta": {"rev": 1}}})]


def test_an_expired_cursor_answers_410_with_a_reload_hint(monkeypatch):
    def serve(*_):
        raise Expired

    monkeypatch.setattr(stream, "serve", serve)
    with pytest.raises(APIError) as caught:
        routes.events(handler(), as_services(fake_server([])), SLUG)
    assert (caught.value.status, caught.value.envelope()) == (
        410,
        {"error": {"code": "cursor_expired", "message": "Cursor no longer retained; reconnect without it to reload"}},
    )


def test_only_a_stream_accept_header_turns_the_events_path_into_a_stream(monkeypatch):
    seen = []
    monkeypatch.setattr(routes, "events", lambda request, server, slug: seen.append(("stream", slug)))
    monkeypatch.setattr(
        resources, "read", lambda state, path, query, cache: seen.append(("collection", path)) or {"data": []}
    )
    server = fake_server([])
    assert routes.dispatch(handler("text/event-stream"), as_services(server)) is None
    assert routes.dispatch(handler(), as_services(server)) == {"data": []}
    assert routes.dispatch(handler("application/json"), as_services(server)) == {"data": []}
    assert seen == [("stream", SLUG), ("collection", "events"), ("collection", "events")]


def test_a_task_work_folder_read_answers_its_lines_with_their_revision():
    server = SimpleNamespace(workspace_tails=lambda slug, task: {"latest_progress": f"{slug} {task}"})
    data = {"latest_progress": f"{SLUG} t1"}
    assert routes.workspace(as_services(server), SLUG, "t1") == {"data": data, "revision": resources.revision(data)}


def test_an_unsafe_task_work_folder_answers_not_found():
    def refuse(slug, task):
        raise ValueError(task)

    with pytest.raises(APIError) as caught:
        routes.workspace(as_services(SimpleNamespace(workspace_tails=refuse)), SLUG, "t 1")
    assert (caught.value.status, caught.value.envelope()) == (
        404,
        {"error": {"code": "resource_missing", "message": "No such task work folder"}},
    )


@pytest.mark.parametrize(
    ("path", "routed"), [("tasks/t1/workspace", True), ("tasks/t1/workspaces", False), ("x/tasks/t1/workspace", False)]
)
def test_only_the_exact_task_work_folder_path_reads_the_folder(path, routed):
    assert bool(routes.WORKSPACE_RE.fullmatch(path)) is routed


def get_request(path, command="GET", sent=None):
    return SimpleNamespace(
        headers={"Host": "h"},
        path=f"/api/v1/ledgers/{SLUG}/{path}",
        command=command,
        exists=lambda slug: True,
        send_media=lambda *args: sent.append(args),
    )


def test_file_reads_need_no_credential_and_come_from_their_own_store():
    sent = []
    anonymous = SimpleNamespace(caller=lambda headers, admin, slug: None, controller=lambda credential: False)
    server = SimpleNamespace(
        **{**vars(fake_server([])), "authority": anonymous, "ledger_media": "media", "ledger_artifacts": "artifacts"}
    )
    for kind in ("media", "artifacts"):
        assert routes.dispatch(get_request(f"files/{kind}/a.png", sent=sent), as_services(server)) is None
    assert sent == [(SLUG, "a.png", "media"), (SLUG, "a.png", "artifacts")]
    with pytest.raises(APIError) as caught:
        routes.dispatch(get_request("files/media/a.png", "DELETE", sent), as_services(server))
    assert (caught.value.status, len(sent)) == (403, 2)


def test_the_model_list_and_swarm_reads_answer_with_their_data(monkeypatch):
    catalog = {"claude": [{"model": "opus"}], "codex": []}
    seen = []
    monkeypatch.setattr(
        resources, "swarm_read", lambda status, path, query: seen.append((status, path, query)) or {"data": status}
    )
    monkeypatch.setattr(resources, "read", lambda state, path, query, cache: seen.append((path, query)) or {"data": []})
    server = SimpleNamespace(
        **{**vars(fake_server([])), "swarm_models": lambda: catalog, "swarm_status": lambda slug: {"slug": slug}}
    )
    models = routes.dispatch(get_request("swarm/models"), as_services(server))
    assert models == {"data": catalog, "revision": resources.revision(catalog)}
    assert routes.dispatch(get_request("swarm/agents?limit=5"), as_services(server)) == {"data": {"slug": SLUG}}
    assert routes.dispatch(get_request("chat?limit=7"), as_services(server)) == {"data": []}
    assert seen == [({"slug": SLUG}, "swarm/agents", {"limit": 5}), ("chat", {"limit": 7})]


def test_only_a_put_to_a_page_write_reaches_the_page_handler():
    written = []
    server = SimpleNamespace(**{**vars(fake_server([])), "MAX_BODY": 100})

    headers = Message()
    headers["Host"] = "h"

    def request(command, path):
        return SimpleNamespace(
            **{**vars(get_request(path, command)), "headers": headers},
            put_ledger=lambda slug: written.append(("saves", slug)),
            put_swarm=lambda slug: written.append(("swarm/controls", slug)),
        )

    routes.dispatch(request("PUT", "saves"), as_services(server))
    routes.dispatch(request("PUT", "swarm/controls"), as_services(server))
    with pytest.raises(APIError) as caught:
        routes.dispatch(request("POST", "saves"), as_services(server))
    assert (caught.value.status, written) == (415, [("saves", SLUG), ("swarm/controls", SLUG)])
