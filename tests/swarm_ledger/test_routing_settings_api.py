import io
import json
import os
import re
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from scripts.routing.settings import FileSettings
from scripts.swarm_ledger import ledger_server
from scripts.swarm_ledger.api import resources, routes, routing
from scripts.swarm_ledger.api.errors import APIError

SLUG = "routing-2026-01-01"
PRINCIPALS = {"admin-token": "", "agent-token": "engineer@1"}


class Headers(dict):
    def get_content_type(self):
        return self.get("Content-Type", "text/plain")


def fake_server(calls=None):
    calls = [] if calls is None else calls

    def principal(admin, slug, token, agent):
        calls.append((admin, slug, token, agent))
        return PRINCIPALS.get(token)

    def token(slug):
        calls.append(("token", slug))
        return "admin-token"

    return SimpleNamespace(
        ALLOWED_HOSTS={"h"},
        ALLOWED_ORIGINS=set(),
        MAX_BODY=10_000,
        core=SimpleNamespace(SLUG_RE=re.compile(r"[a-z0-9-]+"), loads=json.loads),
        authority=SimpleNamespace(principal=principal),
        repository=SimpleNamespace(token=token),
    )


def request(method, payload=None, token="admin-token", slug=SLUG, agent=None):
    raw = b"" if payload is None else json.dumps(payload).encode()
    headers = Headers({"Host": "h", "Content-Type": "application/json", "Content-Length": str(len(raw))})
    if token is not None:
        headers["X-Ledger-Token"] = token
    if slug is not None:
        headers["X-Ledger-Slug"] = slug
    if agent is not None:
        headers["X-Ledger-Agent"] = agent
    return SimpleNamespace(
        headers=headers,
        path="/api/v1/routing/settings",
        command=method,
        rfile=io.BytesIO(raw),
        exists=lambda name: name == SLUG,
    )


@pytest.fixture
def store(tmp_path, monkeypatch):
    settings = FileSettings(tmp_path / "routing-settings.json")
    monkeypatch.setattr(routing, "store", lambda: settings)
    return settings


def test_a_read_answers_every_setting_with_its_revision(store):
    store.set("claude-api-max-sessions", 3, "operator", 1.0)
    reply = routes.dispatch(request("GET", token=None, slug=None), fake_server())
    assert reply["data"] == {"claude-api-weight": 0, "codex-api-weight": 0, "claude-api-max-sessions": 3}
    assert reply["revision"] == resources.revision(reply["data"])


def test_the_operator_writes_weights_and_caps_and_null_clears_a_cap(store, monkeypatch):
    store.set("codex-api-max-sessions", 4, "operator", 1.0)
    monkeypatch.setattr(routing.time, "time", lambda: 123.0)
    reply = routes.dispatch(request("PATCH", {"claude-api-weight": 25, "codex-api-max-sessions": None}), fake_server())
    assert reply["data"] == {"claude-api-weight": 25, "codex-api-weight": 0}
    assert store.all() == reply["data"]
    assert [(e["key"], e["value"], e["actor"], e["at"]) for e in store.history()[1:]] == [
        ("claude-api-weight", 25, "operator", 123.0),
        ("codex-api-max-sessions", None, "operator", 123.0),
    ]


def test_a_write_checks_the_named_ledgers_token_and_passes_the_callers_agent(store):
    calls = []
    routes.dispatch(request("PATCH", {"claude-api-weight": 5}, agent="operator-page"), fake_server(calls))
    assert calls == [("token", SLUG), ("admin-token", SLUG, "admin-token", "operator-page")]


def test_the_store_opens_on_the_swarm_redis_client_and_the_process_environment(monkeypatch):
    from scripts.routing import place
    from scripts.routing import settings as settings_module

    client, seen = object(), []
    monkeypatch.setattr(place, "_client", lambda environ: seen.append(environ) or client)
    monkeypatch.setattr(settings_module, "open_store", lambda opened, environ: (opened, environ))
    opened, environ = routing.store()
    assert opened is client
    assert environ is os.environ
    assert seen == [os.environ]


@pytest.mark.parametrize(
    ("token", "slug", "message"),
    [
        ("agent-token", SLUG, "Routing settings need the operator"),
        ("wrong", SLUG, "Missing or wrong ledger credential"),
        (None, SLUG, "Missing or wrong ledger credential"),
        ("admin-token", None, "Missing or wrong ledger credential"),
        ("admin-token", "other-ledger", "Missing or wrong ledger credential"),
    ],
)
def test_a_write_without_the_operator_principal_is_refused_and_writes_nothing(store, token, slug, message):
    with pytest.raises(APIError) as caught:
        routes.dispatch(request("PATCH", {"claude-api-weight": 50}, token=token, slug=slug), fake_server())
    error = caught.value.envelope()["error"]
    assert (caught.value.status, error["code"], error["message"]) == (403, "forbidden", message)
    assert store.history() == []


@pytest.mark.parametrize(
    "payload",
    [
        {"claude-api-weight": 101},
        {"claude-api-weight": 20, "codex-api-max-sessions": -1},
        {"claude-api-weight": "25"},
        {"claude-api-weight": True},
        {"unknown-key": 1},
        {},
        [],
    ],
)
def test_an_invalid_write_is_refused_before_any_key_is_written(store, payload):
    with pytest.raises(APIError) as caught:
        routes.dispatch(request("PATCH", payload), fake_server())
    assert (caught.value.status, caught.value.code) == (400, "schema_invalid")
    assert store.history() == []


def test_an_invalid_value_names_its_key(store):
    with pytest.raises(APIError) as caught:
        routes.dispatch(request("PATCH", {"codex-api-weight": 150}), fake_server())
    assert caught.value.envelope()["error"]["message"] == "Invalid value for codex-api-weight"


def test_other_methods_on_the_settings_resource_are_not_allowed(store):
    with pytest.raises(APIError) as caught:
        routes.dispatch(request("POST", {"claude-api-weight": 5}), fake_server())
    error = caught.value.envelope()["error"]
    assert (caught.value.status, error["code"], error["message"]) == (405, "method_not_allowed", "Use GET or PATCH")


@pytest.fixture
def http_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), ledger_server.Handler)
    threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def patch_over_http(port, path, payload):
    origin = f"http://127.0.0.1:{ledger_server.PORT}"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode(),
        headers={"Host": f"127.0.0.1:{ledger_server.PORT}", "Origin": origin, "Content-Type": "application/json"},
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.headers["Content-Type"], resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers["Content-Type"], exc.read().decode()


def test_the_server_answers_a_patch_on_the_api_through_the_routing_resource(store, http_server, monkeypatch):
    monkeypatch.setattr(routing, "_principal", lambda handler, server: "")
    status, ctype, body = patch_over_http(http_server, "/api/v1/routing/settings", {"codex-api-weight": 30})
    assert (status, ctype) == (200, "application/json")
    assert json.loads(body)["data"]["codex-api-weight"] == 30
    assert store.get("codex-api-weight") == 30


def test_the_server_refuses_a_patch_outside_the_api(store, http_server):
    assert patch_over_http(http_server, "/api/routing/settings", {"codex-api-weight": 30}) == (
        404,
        "text/plain",
        "not found",
    )
    assert store.history() == []
