from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.events import nats_server


def test_a_listening_port_does_not_make_an_unstarted_child_ready(monkeypatch, tmp_path):
    process = SimpleNamespace(
        pid=123,
        returncode=1,
        poll=Mock(side_effect=[None, 1]),
        terminate=Mock(),
        wait=Mock(),
    )
    listener = Mock()
    monkeypatch.setattr(nats_server, "binary", lambda: "nats-server")
    monkeypatch.setattr(nats_server, "free_port", lambda: 4222, raising=False)
    monkeypatch.setattr(nats_server.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(nats_server.socket, "create_connection", listener)

    with pytest.raises(RuntimeError, match="nats-server exited with 1"):
        with nats_server.running(tmp_path / "jetstream"):
            pytest.fail("the fixture accepted another process's listener")

    listener.assert_not_called()
    process.terminate.assert_called_once_with()
    process.wait.assert_called_once_with(10)
