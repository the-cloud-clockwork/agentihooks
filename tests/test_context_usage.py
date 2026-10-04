import json

import pytest

pytestmark = pytest.mark.unit

_CONTEXT_WINDOW = {"used_percentage": 25.0, "context_window_size": 200000}


def test_record_writes_snapshot_and_reader_returns_used_tokens(monkeypatch, tmp_path):
    import hooks.context.context_usage as context_usage

    monkeypatch.setattr(context_usage, "AGENTIHOOKS_HOME", tmp_path)
    context_usage.record_context_usage("session-1", _CONTEXT_WINDOW)

    snapshot = json.loads((tmp_path / "context_usage" / "session-1.json").read_text())
    assert snapshot["used_tokens"] == 50000
    assert snapshot["context_window_size"] == 200000
    assert snapshot["used_pct"] == 25.0
    assert isinstance(snapshot["updated_at"], float)
    assert context_usage.used_tokens("session-1") == 50000


def test_reader_returns_latest_value(monkeypatch, tmp_path):
    import hooks.context.context_usage as context_usage

    monkeypatch.setattr(context_usage, "AGENTIHOOKS_HOME", tmp_path)
    context_usage.record_context_usage("session-1", _CONTEXT_WINDOW)
    context_usage.record_context_usage("session-1", {"used_percentage": 50, "context_window_size": 200000})

    assert context_usage.used_tokens("session-1") == 100000


def test_reader_returns_none_without_snapshot(monkeypatch, tmp_path):
    import hooks.context.context_usage as context_usage

    monkeypatch.setattr(context_usage, "AGENTIHOOKS_HOME", tmp_path)

    assert context_usage.used_tokens("missing") is None
    assert context_usage.used_tokens("") is None


@pytest.mark.parametrize(
    "session_id,window", [("", _CONTEXT_WINDOW), ("s", {}), ("s", {"used_percentage": 5}), ("s", None)]
)
def test_record_ignores_unusable_input(monkeypatch, tmp_path, session_id, window):
    import hooks.context.context_usage as context_usage

    monkeypatch.setattr(context_usage, "AGENTIHOOKS_HOME", tmp_path)
    context_usage.record_context_usage(session_id, window)

    assert not (tmp_path / "context_usage").exists()
