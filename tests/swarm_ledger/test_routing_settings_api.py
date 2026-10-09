import io
import json
import re
from types import SimpleNamespace

import pytest

from scripts.routing.settings import FileSettings
from scripts.swarm_ledger.api import routes, routing
from scripts.swarm_ledger.api.errors import APIError

SLUG = "routing-2026-01-01"
PRINCIPALS = {"admin-token": "", "agent-token": "engineer@1"}


class Headers(dict):
    def get_content_type(self):
        return self.get("Content-Type", "text/plain")


def fake_server():
    return SimpleNamespace(
        ALLOWED_HOSTS={"h"},
        ALLOWED_ORIGINS=set(),
        MAX_BODY=10_000,
        core=SimpleNamespace(SLUG_RE=re.compile(r"[a-z0-9-]+"), loads=json.loads),
        authority=SimpleNamespace(principal=lambda admin, slug, token, agent: PRINCIPALS.get(token)),
        repository=SimpleNamespace(token=lambda slug: "admin-token"),
    )


def request(method, payload=None, token="admin-token", slug=SLUG):
    raw = b"" if payload is None else json.dumps(payload).encode()
    headers = Headers({"Host": "h", "Content-Type": "application/json", "Content-Length": str(len(raw))})
    if token is not None:
        headers["X-Ledger-Token"] = token
    if slug is not None:
        headers["X-Ledger-Slug"] = slug
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
    assert reply["revision"]


def test_the_operator_writes_weights_and_caps_and_null_clears_a_cap(store):
    store.set("codex-api-max-sessions", 4, "operator", 1.0)
    reply = routes.dispatch(request("PATCH", {"claude-api-weight": 25, "codex-api-max-sessions": None}), fake_server())
    assert reply["data"] == {"claude-api-weight": 25, "codex-api-weight": 0}
    assert store.all() == reply["data"]
    assert [(e["key"], e["value"], e["actor"]) for e in store.history()[1:]] == [
        ("claude-api-weight", 25, "operator"),
        ("codex-api-max-sessions", None, "operator"),
    ]


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
    assert (caught.value.status, caught.value.envelope()["error"]["message"]) == (403, message)
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
    assert caught.value.status == 405
