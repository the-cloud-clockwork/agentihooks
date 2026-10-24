from unittest.mock import Mock, call

import pytest

from hooks.context import amygdala_hook


@pytest.fixture
def signal_file(tmp_path, monkeypatch):
    path = tmp_path / "signal.md"
    monkeypatch.setattr(amygdala_hook, "_SIGNAL_PATH", str(path))
    monkeypatch.setattr("hooks._brain_http.brain_http_enabled", lambda: False)
    get = Mock()
    monkeypatch.setattr("hooks._brain_http.get", get)
    reconcile = Mock()
    monkeypatch.setattr(amygdala_hook, "reconcile_channel_broadcasts", reconcile)
    return path, get, reconcile


def test_unconfigured_signal_leaves_broadcasts_unchanged(signal_file, monkeypatch):
    _, get, reconcile = signal_file
    monkeypatch.setattr(amygdala_hook, "_SIGNAL_PATH", "")

    amygdala_hook.check_amygdala("session")

    get.assert_not_called()
    reconcile.assert_not_called()


def test_missing_signal_clears_broadcasts(signal_file):
    _, get, reconcile = signal_file

    amygdala_hook.check_amygdala("session")

    get.assert_not_called()
    reconcile.assert_called_once_with("amygdala", [])


@pytest.mark.parametrize(
    "content, message, severity",
    [
        (
            "---\nseverity: warning\ntitle: Service alert\n---\nWorker unavailable",
            "[Service alert]\n\nWorker unavailable",
            "warning",
        ),
        ("Worker unavailable", "[AMYGDALA ALERT]\n\nWorker unavailable", "critical"),
    ],
)
def test_signal_file_publishes_broadcast(signal_file, content, message, severity):
    path, get, reconcile = signal_file
    path.write_text(content)

    amygdala_hook.check_amygdala("session")

    get.assert_not_called()
    reconcile.assert_called_once_with(
        "amygdala",
        [{"message": message, "severity": severity, "persistent": True, "source": "amygdala-hook"}],
    )


@pytest.mark.parametrize("payload, cleared", [(None, []), ({"active": False}, [call("amygdala", [])])])
def test_a_brain_answer_leaves_the_signal_file_unread(signal_file, monkeypatch, payload, cleared):
    path, get, reconcile = signal_file
    path.write_text("Worker unavailable")
    monkeypatch.setattr("hooks._brain_http.brain_http_enabled", lambda: True)
    get.return_value = payload

    amygdala_hook.check_amygdala("session")

    get.assert_called_once_with("/signal")
    assert reconcile.call_args_list == cleared
