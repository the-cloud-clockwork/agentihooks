"""Unit tests for Phase 7 brain HTTP client paths.

Covers:
- hooks._brain_http.brain_http_enabled / get / post
- hooks.context.brain_adapter.HttpBrainSource
- hooks.context.amygdala_hook._check_via_http
- hooks.context.brain_writer_hook._publish_to_http

All tests mock the network layer (urllib.request.urlopen) — no real sockets.
"""

from __future__ import annotations

import importlib
import json
from io import BytesIO
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    """Every test uses a tmp AGENTIHOOKS_HOME with no .env files.

    Without this the config._load_user_env pass reads ~/.agentihooks/*.env on
    every module reload and overrides monkeypatch values.
    """
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    yield


def _reload_config_with_env(monkeypatch, **env: str):
    """Force hooks.config to re-read env after monkeypatch."""
    # Reset both so no stale tokens leak through from the operator's real env.
    monkeypatch.setenv("BRAIN_HTTP_TOKEN", "")
    monkeypatch.setenv("KB_ROUTER_TOKEN", "")
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    import hooks.config as cfg

    importlib.reload(cfg)
    return cfg


def _fake_http_response(payload: dict[str, Any], status: int = 200):
    """Return a context-manager-compatible response object."""
    body = json.dumps(payload).encode("utf-8")
    resp = MagicMock()
    resp.read.return_value = body
    resp.status = status
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# ── _brain_http ───────────────────────────────────────────────────────


def test_brain_http_disabled_when_url_empty(monkeypatch):
    _reload_config_with_env(monkeypatch, BRAIN_URL="", KB_ROUTER_TOKEN="")
    import hooks._brain_http as bh

    importlib.reload(bh)
    assert bh.brain_http_enabled() is False
    assert bh.get("/feed") is None


def test_brain_http_get_sends_bearer(monkeypatch):
    _reload_config_with_env(monkeypatch, BRAIN_URL="http://kb:8080", KB_ROUTER_TOKEN="t0ken")
    import hooks._brain_http as bh

    importlib.reload(bh)

    captured: dict[str, Any] = {}

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = dict(req.header_items())
        return _fake_http_response({"hot_arcs": [], "inject_blocks": [], "entries": []})

    with patch("hooks._brain_http.urlopen", side_effect=fake_urlopen):
        out = bh.get("/feed")
    assert out == {"hot_arcs": [], "inject_blocks": [], "entries": []}
    assert captured["url"].endswith("/feed")
    assert captured["method"] == "GET"
    assert captured["headers"].get("Authorization") == "Bearer t0ken"


def test_brain_http_post_serializes_body_and_idem_key(monkeypatch):
    _reload_config_with_env(monkeypatch, BRAIN_URL="http://kb:8080", KB_ROUTER_TOKEN="t0ken")
    import hooks._brain_http as bh

    importlib.reload(bh)
    captured: dict[str, Any] = {}

    def fake_urlopen(req, timeout=0):
        captured["body"] = req.data
        captured["headers"] = dict(req.header_items())
        captured["method"] = req.get_method()
        return _fake_http_response({"ok": True})

    with patch("hooks._brain_http.urlopen", side_effect=fake_urlopen):
        out = bh.post("/marker", {"type": "lesson", "content": "x"}, idempotency_key="k-1")
    assert out == {"ok": True}
    assert captured["method"] == "POST"
    body = json.loads(captured["body"].decode())
    assert body["type"] == "lesson"
    # urllib lower-cases custom headers
    assert any(k.lower() == "x-idempotency-key" for k in captured["headers"])


def test_brain_http_swallows_http_error(monkeypatch):
    _reload_config_with_env(monkeypatch, BRAIN_URL="http://kb:8080", KB_ROUTER_TOKEN="t")
    from urllib.error import HTTPError

    import hooks._brain_http as bh

    importlib.reload(bh)

    def boom(*_a, **_kw):
        raise HTTPError(url="http://kb:8080/feed", code=503, msg="bad", hdrs={}, fp=BytesIO(b"down"))

    with patch("hooks._brain_http.urlopen", side_effect=boom):
        assert bh.get("/feed") is None


# ── brain_adapter.HttpBrainSource ─────────────────────────────────────


def test_http_brain_source_parses_feed_payload(monkeypatch):
    _reload_config_with_env(monkeypatch, BRAIN_URL="http://kb:8080", KB_ROUTER_TOKEN="t")
    import hooks._brain_http as bh
    import hooks.context.brain_adapter as ba

    importlib.reload(bh)
    importlib.reload(ba)

    payload = {
        "hot_arcs": [
            {"id": "hot-arcs-today", "title": "Hot", "content": "body1", "priority": 10},
        ],
        "inject_blocks": [
            {"id": "inject", "title": "Inject", "content": "body2", "priority": 9},
        ],
        "entries": [
            {"id": "intent", "title": "Intent", "content": "body3", "priority": 7},
        ],
        "generated_at": "2026-04-22T00:00:00+00:00",
        "hash": "abc",
    }
    with patch("hooks._brain_http.urlopen", return_value=_fake_http_response(payload)):
        entries = ba.HttpBrainSource().fetch()

    assert [e.id for e in entries] == ["hot-arcs-today", "inject", "intent"]
    assert entries[0].priority == 10
    assert entries[1].content == "body2"


def test_http_brain_source_returns_empty_on_failure(monkeypatch):
    _reload_config_with_env(monkeypatch, BRAIN_URL="http://kb:8080", KB_ROUTER_TOKEN="t")
    import hooks._brain_http as bh
    import hooks.context.brain_adapter as ba

    importlib.reload(bh)
    importlib.reload(ba)

    with patch("hooks._brain_http.urlopen", side_effect=OSError("boom")):
        assert ba.HttpBrainSource().fetch() == []


def test_brain_adapter_selects_http_when_enabled(monkeypatch):
    _reload_config_with_env(monkeypatch, BRAIN_URL="http://kb:8080", KB_ROUTER_TOKEN="t")
    import hooks._brain_http as bh
    import hooks.context.brain_adapter as ba

    importlib.reload(bh)
    importlib.reload(ba)
    source = ba._get_source()
    assert isinstance(source, ba.HttpBrainSource)


def test_brain_adapter_falls_back_to_file(monkeypatch):
    _reload_config_with_env(monkeypatch, BRAIN_URL="", BRAIN_SOURCE_TYPE="file", BRAIN_SOURCE_PATH="/tmp/nowhere")
    import hooks._brain_http as bh
    import hooks.context.brain_adapter as ba

    importlib.reload(bh)
    importlib.reload(ba)
    source = ba._get_source()
    assert isinstance(source, ba.FileBrainSource)


# ── amygdala_hook._check_via_http ─────────────────────────────────────


def test_amygdala_http_publishes_broadcast_on_active_signal(monkeypatch):
    _reload_config_with_env(monkeypatch, BRAIN_URL="http://kb:8080", KB_ROUTER_TOKEN="t")
    import hooks._brain_http as bh
    import hooks.context.amygdala_hook as ah

    importlib.reload(bh)
    importlib.reload(ah)

    payload = {
        "active": True,
        "severity": "critical",
        "title": "Prod broke",
        "content": "publisher-0 OOMKilled",
        "hash": "h1",
        "last_updated": "2026-04-22T00:00:00Z",
    }
    called: dict[str, Any] = {}

    def fake_create(**kwargs):
        called.update(kwargs)
        return "msg-1"

    with (
        patch("hooks._brain_http.urlopen", return_value=_fake_http_response(payload)),
        patch("hooks.context.amygdala_hook.create_broadcast", side_effect=fake_create),
        patch("hooks.context.amygdala_hook.clear_broadcasts"),
    ):
        assert ah._check_via_http() is True

    assert called["severity"] == "critical"
    assert "Prod broke" in called["message"]
    assert called["channel"] == "amygdala"


def test_amygdala_http_clears_when_signal_absent(monkeypatch):
    _reload_config_with_env(monkeypatch, BRAIN_URL="http://kb:8080", KB_ROUTER_TOKEN="t")
    import hooks._brain_http as bh
    import hooks.context.amygdala_hook as ah

    importlib.reload(bh)
    importlib.reload(ah)
    ah._last_hash = "stale"

    payload = {"active": False, "severity": None, "title": None, "content": None, "hash": None}
    cleared: dict[str, Any] = {}

    def fake_clear(channel):
        cleared["channel"] = channel

    with (
        patch("hooks._brain_http.urlopen", return_value=_fake_http_response(payload)),
        patch("hooks.context.amygdala_hook.clear_broadcasts", side_effect=fake_clear),
        patch("hooks.context.amygdala_hook.create_broadcast"),
    ):
        assert ah._check_via_http() is True

    assert cleared.get("channel") == "amygdala"
    assert ah._last_hash == ""


def test_amygdala_http_dedups_on_same_hash(monkeypatch):
    _reload_config_with_env(monkeypatch, BRAIN_URL="http://kb:8080", KB_ROUTER_TOKEN="t")
    import hooks._brain_http as bh
    import hooks.context.amygdala_hook as ah

    importlib.reload(bh)
    importlib.reload(ah)
    ah._last_hash = "h1"

    payload = {"active": True, "severity": "warning", "title": "t", "content": "b", "hash": "h1"}
    with (
        patch("hooks._brain_http.urlopen", return_value=_fake_http_response(payload)),
        patch("hooks.context.amygdala_hook.create_broadcast") as create,
        patch("hooks.context.amygdala_hook.clear_broadcasts"),
    ):
        ah._check_via_http()
    assert create.called is False


# ── brain_writer_hook._publish_to_http ────────────────────────────────


def test_brain_writer_http_posts_each_marker(monkeypatch):
    _reload_config_with_env(monkeypatch, BRAIN_URL="http://kb:8080", KB_ROUTER_TOKEN="t")
    import hooks._brain_http as bh
    import hooks.context.brain_writer_hook as bw

    importlib.reload(bh)
    importlib.reload(bw)

    markers = [
        {"type": "lesson", "content": "one", "attrs": {"source": "test"}},
        {"type": "milestone", "content": "two", "attrs": {"source": "test"}},
    ]
    calls: list[Any] = []

    def fake_urlopen(req, timeout=0):
        calls.append(req.full_url)
        return _fake_http_response({"ok": True})

    with patch("hooks._brain_http.urlopen", side_effect=fake_urlopen):
        count, failed = bw._publish_to_http(markers, session_id="sess-abc")
    assert count == 2
    assert failed == []
    assert all(c.endswith("/marker") for c in calls)


def test_brain_writer_http_failure_returns_pending(monkeypatch):
    _reload_config_with_env(monkeypatch, BRAIN_URL="http://kb:8080", KB_ROUTER_TOKEN="t")
    import hooks._brain_http as bh
    import hooks.context.brain_writer_hook as bw

    importlib.reload(bh)
    importlib.reload(bw)

    markers = [{"type": "lesson", "content": "x", "attrs": {}}]
    with patch("hooks._brain_http.urlopen", side_effect=OSError("boom")):
        count, failed = bw._publish_to_http(markers, session_id="s")
    assert count == 0
    assert failed == markers


def test_brain_writer_http_noop_when_url_unset(monkeypatch):
    _reload_config_with_env(monkeypatch, BRAIN_URL="", KB_ROUTER_TOKEN="")
    import hooks._brain_http as bh
    import hooks.context.brain_writer_hook as bw

    importlib.reload(bh)
    importlib.reload(bw)

    markers = [{"type": "lesson", "content": "x", "attrs": {}}]
    count, failed = bw._publish_to_http(markers, session_id="s")
    assert count == 0
    assert failed == markers
