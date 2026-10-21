import socket
import uuid

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def sd_notify():
    from scripts.swarm import sd_notify

    return sd_notify


@pytest.fixture
def listener(tmp_path):
    path = str(tmp_path / "notify")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.bind(path)
        sock.settimeout(5)
        yield path, sock


def test_a_message_reaches_the_socket_named_by_notify_socket(sd_notify, listener):
    path, sock = listener

    assert sd_notify.notify("WATCHDOG=1", {"NOTIFY_SOCKET": path}) is True
    assert sock.recv(64) == b"WATCHDOG=1"


def test_an_abstract_socket_name_is_reached_through_its_leading_nul(sd_notify):
    name = f"agentihooks-notify-{uuid.uuid4().hex}"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.bind("\0" + name)
        sock.settimeout(5)

        assert sd_notify.notify("READY=1", {"NOTIFY_SOCKET": "@" + name}) is True
        assert sock.recv(64) == b"READY=1"


@pytest.mark.parametrize("environ", [{}, {"NOTIFY_SOCKET": ""}])
def test_without_a_notify_socket_nothing_is_sent(sd_notify, environ):
    assert sd_notify.notify("WATCHDOG=1", environ) is False


def test_the_process_environment_names_the_socket_by_default(sd_notify, listener, monkeypatch):
    path, sock = listener
    monkeypatch.setenv("NOTIFY_SOCKET", path)

    assert sd_notify.notify("WATCHDOG=1") is True
    assert sock.recv(64) == b"WATCHDOG=1"
