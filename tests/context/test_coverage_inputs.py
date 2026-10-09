from unittest.mock import Mock
from urllib.error import URLError

import pytest


@pytest.mark.parametrize(
    "enabled, session_id, context",
    [(True, "session", "project context"), (True, "session", ""), (True, "", ""), (False, "session", "")],
)
def test_brain_session_start_controls_refresh_and_project_context(enabled, session_id, context, monkeypatch):
    from hooks import config
    from hooks.context import brain_adapter

    monkeypatch.setattr(config, "BRAIN_ENABLED", enabled)
    refresh = Mock(return_value=True)
    monkeypatch.setattr(brain_adapter, "force_refresh", refresh)
    project = Mock(return_value="project")
    monkeypatch.setattr("hooks.context.project_identity.resolve_project", project)
    record = Mock()
    monkeypatch.setattr("hooks.context.project_sessions.record_session", record)
    lookup = Mock(return_value=context)
    monkeypatch.setattr("hooks.context.project_cache.project_context", lookup)
    inject = Mock()
    monkeypatch.setattr("hooks.common.inject_context", inject)

    assert brain_adapter.inject_on_session_start(session_id, "repo") is enabled

    if enabled:
        refresh.assert_called_once_with()
    else:
        refresh.assert_not_called()
    if enabled and session_id:
        project.assert_called_once_with("repo")
        record.assert_called_once_with(session_id, "project")
        lookup.assert_called_once_with(session_id, "repo")
    else:
        project.assert_not_called()
        record.assert_not_called()
        lookup.assert_not_called()
    if enabled and context:
        inject.assert_called_once_with(context, skip_compression=True)
    else:
        inject.assert_not_called()


def test_brain_http_without_authentication(monkeypatch):
    from hooks import _brain_http, config

    monkeypatch.setattr(config, "BRAIN_HTTP_TOKEN", "")

    assert _brain_http._auth_headers() == {}


def test_brain_http_url_error_is_logged_and_returns_no_payload(monkeypatch):
    from hooks import _brain_http, config

    monkeypatch.setattr(config, "BRAIN_URL", "http://brain.invalid")
    monkeypatch.setattr(config, "BRAIN_HTTP_TOKEN", "")
    monkeypatch.setattr(config, "BRAIN_HTTP_TIMEOUT", 1)
    request = Mock(side_effect=URLError("offline"))
    monkeypatch.setattr(_brain_http, "urlopen", request)
    log = Mock()
    monkeypatch.setattr(_brain_http, "log", log)

    assert _brain_http.get("/feed") is None

    request.assert_called_once()
    assert request.call_args.args[0].full_url == "http://brain.invalid/feed"
    assert request.call_args.kwargs == {"timeout": 1.0}
    log.assert_called_once_with(
        "brain_http: url error", {"method": "GET", "url": "http://brain.invalid/feed", "error": "offline"}
    )


@pytest.mark.parametrize("content", ["", '{"type": "session_meta"}\n', '{"rate_limits": null}\n'])
def test_codex_rollout_without_quota_returns_none(tmp_path, content):
    from scripts.codex_quota import _last_in

    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(content)

    assert _last_in(rollout) is None
