import pytest

from hooks import _redis, config, hook_manager
from hooks.context import documentation_session
from hooks.lifecycle import guard


@pytest.mark.parametrize("target", ["claude", "codex", "copilot"])
def test_every_harness_session_end_dispatches_documentation_work(tmp_path, monkeypatch, target):
    calls = []
    monkeypatch.setenv("AGENTIHOOKS_TARGET", target)
    monkeypatch.setattr(documentation_session, "end", calls.append)
    monkeypatch.setattr(guard, "session_event", lambda: None)
    monkeypatch.setattr(_redis, "get_redis", lambda: None)
    monkeypatch.setattr(hook_manager, "_request_trace_flush", lambda *args: None)
    monkeypatch.setattr(config, "BROADCAST_ENABLED", False)
    payload = {"session_id": "hook-fixture", "cwd": str(tmp_path)}
    hook_manager.on_session_end(payload)
    assert calls == [payload]
