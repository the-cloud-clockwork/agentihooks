import json
from pathlib import Path
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


def test_a_fixture_uses_only_its_childs_published_listener(monkeypatch, tmp_path):
    process = SimpleNamespace(pid=123, poll=Mock(return_value=None), terminate=Mock(), wait=Mock())
    launched = []

    def launch(command, **kwargs):
        launched.append(command)
        ports = Path(command[command.index("--ports_file_dir") + 1])
        (ports / "nats-server_456.ports").write_text(json.dumps({"nats": ["nats://127.0.0.1:4222"]}))
        (ports / "nats-server_123.ports").write_text(json.dumps({"nats": ["nats://127.0.0.1:5123"]}))
        return process

    monkeypatch.setattr(nats_server, "binary", lambda: "/bin/nats-server")
    monkeypatch.setattr(nats_server.subprocess, "Popen", launch)
    with nats_server.running(tmp_path / "jetstream") as url:
        assert url == "nats://127.0.0.1:5123"
        process.terminate.assert_not_called()

    assert launched[0][launched[0].index("-p") + 1] == "-1"
    process.terminate.assert_called_once_with()
    process.wait.assert_called_once_with(10)


def test_a_partial_ports_file_waits_for_the_childs_complete_publication(monkeypatch, tmp_path):
    ports = tmp_path / "nats-server_123.ports"
    ports.write_text('{"nats":')
    process = SimpleNamespace(poll=Mock(return_value=None))
    pause = Mock(side_effect=lambda _: ports.write_text(json.dumps({"nats": ["nats://127.0.0.1:5123"]})))
    monkeypatch.setattr(nats_server.time, "sleep", pause)

    assert nats_server._wait_url(ports, process) == "nats://127.0.0.1:5123"
    pause.assert_called_once_with(0.02)
