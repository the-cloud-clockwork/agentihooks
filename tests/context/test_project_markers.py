import json
from datetime import datetime, timezone
from unittest.mock import patch

from hooks.context.brain_writer_hook import _drain_outbox, _marker_request, _write_to_outbox
from hooks.context.project_identity import ProjectIdentity
from hooks.context.project_sessions import record_session


def test_direct_and_outbox_keep_session_identity(monkeypatch, tmp_path):
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", tmp_path / "state")
    identity = ProjectIdentity("alpha", "org/alpha", "fix", "/work/alpha/fix")
    record_session("sid", identity)
    marker = {"type": "lesson", "content": "One lesson.", "attrs": {}, "at": datetime.now(timezone.utc).isoformat()}
    body, idem = _marker_request(marker, "sid")
    assert all(body["attrs"][key] == value for key, value in identity.attributes().items() if value)
    outbox = tmp_path / "outbox"
    _write_to_outbox([marker], "sid", str(outbox))
    saved = json.loads(next(outbox.glob("*.json")).read_text())
    assert saved["attrs"]["repo"] == "org/alpha"
    with (
        patch("hooks._brain_http.brain_http_enabled", return_value=True),
        patch("hooks._brain_http.post", return_value={"ok": True}) as post,
    ):
        assert _drain_outbox(str(outbox)) == 1
    assert post.call_args.kwargs["body"]["attrs"]["repo"] == "org/alpha"
    assert post.call_args.kwargs["idempotency_key"] == idem


def test_model_attribute_wins_without_a_scope_log(monkeypatch, tmp_path):
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.setenv("AGENTIHOOKS_SESSION_SCOPE", "0")
    record_session("sid", ProjectIdentity("alpha", "org/alpha"))
    body, _ = _marker_request({"type": "lesson", "content": "lesson", "attrs": {"project": "explicit"}}, "sid")
    assert body["attrs"]["project"] == "explicit"


def test_explicit_project_id_wins_over_the_scope_log(monkeypatch, tmp_path):
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", tmp_path)
    record_session("sid", ProjectIdentity("alpha", "org/alpha", project_id="github.com/org/alpha"))
    attrs = {"project": "explicit", "project_id": "github.com/org/explicit"}
    marker = {"type": "lesson", "content": "lesson", "attrs": attrs, "at": datetime.now(timezone.utc).isoformat()}
    body, _ = _marker_request(marker, "sid")
    assert (body["attrs"]["project"], body["attrs"]["project_id"]) == ("explicit", "github.com/org/explicit")
    assert body["attrs"]["attribution"] == "explicit"


def test_unknown_outbox_does_not_take_drainers_project(monkeypatch, tmp_path):
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", tmp_path / "state")
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    (outbox / "legacy.json").write_text(json.dumps({"type": "lesson", "content": "old", "session_id": "unknown"}))
    with (
        patch("hooks._brain_http.brain_http_enabled", return_value=True),
        patch("hooks._brain_http.post", return_value={"ok": True}) as post,
        patch("hooks.context.project_identity.resolve_project", return_value=None) as resolve,
    ):
        _drain_outbox(str(outbox))
    resolve.assert_called_once_with("", {})
    assert "project" not in post.call_args.kwargs["body"]["attrs"]
