import io
from unittest.mock import Mock

import pytest

from hooks import hook_manager


def test_classifier_child_returns_before_stdin_dispatch_and_telemetry(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_CHILD", "1")
    stdin, handler, flush, exit_process = Mock(), Mock(), Mock(), Mock()
    monkeypatch.setattr(hook_manager.sys, "stdin", stdin)
    monkeypatch.setitem(hook_manager.EVENT_HANDLERS, "SessionStart", handler)
    monkeypatch.setattr(hook_manager.otel, "flush", flush)
    monkeypatch.setattr(hook_manager.os, "_exit", exit_process)
    hook_manager.main()
    stdin.read.assert_not_called()
    handler.assert_not_called()
    flush.assert_not_called()
    exit_process.assert_not_called()


@pytest.mark.parametrize("value", [None, "0", "true"])
def test_ordinary_hook_still_dispatches(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("AGENTIHOOKS_CLASSIFIER_CHILD", raising=False)
    else:
        monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_CHILD", value)
    handler = Mock()
    monkeypatch.setattr(hook_manager.sys, "stdin", io.StringIO('{"hook_event_name":"SessionStart"}'))
    monkeypatch.setitem(hook_manager.EVENT_HANDLERS, "SessionStart", handler)
    monkeypatch.setattr(hook_manager.otel, "flush", Mock())
    monkeypatch.setattr(hook_manager.os, "_exit", Mock())
    hook_manager.main()
    handler.assert_called_once_with({"hook_event_name": "SessionStart"})
