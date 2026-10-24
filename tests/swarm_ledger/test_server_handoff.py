import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.swarm_ledger import server_handoff

ROOT = Path(__file__).resolve().parents[2]
SUCCESSOR = (
    "import socket\n"
    "from scripts.swarm_ledger import server_handoff\n"
    "listener = server_handoff.inherited()\n"
    "server_handoff.announce(b'{\"demo\": 6}')\n"
    "client, _ = listener.accept()\n"
    "client.sendall(b'successor')\n"
    "client.close()\n"
)


@pytest.fixture
def listener():
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen(8)
        yield held


def test_a_successor_that_reports_ready_serves_on_the_inherited_socket(listener, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    port = listener.getsockname()[1]
    assert server_handoff.hand_off(listener, [sys.executable, "-c", SUCCESSOR], timeout=30) == b'{"demo": 6}'
    listener.close()
    with socket.create_connection(("127.0.0.1", port), timeout=10) as client:
        assert client.recv(64) == b"successor"


def test_a_successor_that_dies_before_ready_leaves_the_old_server_in_charge(listener, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    assert server_handoff.hand_off(listener, [sys.executable, "-c", "raise SystemExit(3)"], timeout=30) is None
    assert listener.fileno() >= 0


def test_a_successor_that_never_reports_is_killed_after_the_wait(listener, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    spawned = []
    popen = subprocess.Popen

    def recorded(*args, **kwargs):
        spawned.append(popen(*args, **kwargs))
        return spawned[-1]

    script = "import time\ntime.sleep(5)\n"
    assert server_handoff.hand_off(listener, [sys.executable, "-c", script], recorded, timeout=0.2) is None
    assert spawned[0].returncode == -signal.SIGKILL


def test_the_successor_gets_the_socket_and_the_ready_pipe_by_number(listener, monkeypatch):
    monkeypatch.setenv("KEEP", "1")
    seen = {}

    def popen(argv, **kwargs):
        seen.update(kwargs, argv=argv)
        os.write(int(kwargs["env"][server_handoff.READY_FD]), server_handoff.READY)
        return subprocess.Popen([sys.executable, "-c", ""])

    assert server_handoff.hand_off(listener, ["serve"], popen, timeout=5) == b""
    env = seen["env"]
    assert seen["argv"] == ["serve"]
    assert env["KEEP"] == "1"
    assert env[server_handoff.LISTEN_FD] == str(listener.fileno())
    assert seen["pass_fds"] == (listener.fileno(), int(env[server_handoff.READY_FD]))
    assert seen["stdin"] is subprocess.DEVNULL
    assert seen["start_new_session"] is True


def test_the_successor_gets_the_extra_settings_over_the_inherited_ones(listener, monkeypatch):
    monkeypatch.setenv("KEEP", "1")
    monkeypatch.setenv("LEDGER_WATCHED", "old")
    seen = {}

    def popen(argv, **kwargs):
        seen.update(kwargs["env"])
        os.write(int(kwargs["env"][server_handoff.READY_FD]), server_handoff.READY)
        return subprocess.Popen([sys.executable, "-c", ""])

    extra = {"LEDGER_WATCHED": "demo", server_handoff.LISTEN_FD: "99"}
    assert server_handoff.hand_off(listener, ["serve"], popen, timeout=5, extra=extra) == b""
    assert seen["KEEP"] == "1"
    assert seen["LEDGER_WATCHED"] == "demo"
    assert seen[server_handoff.LISTEN_FD] == str(listener.fileno())


def test_a_failed_spawn_closes_both_pipe_ends(listener, monkeypatch):
    opened = []
    pipe = os.pipe

    def recorded():
        opened.extend(pipe())
        return tuple(opened)

    monkeypatch.setattr(server_handoff.os, "pipe", recorded)

    def popen(argv, **kwargs):
        raise OSError("no such interpreter")

    with pytest.raises(OSError, match="no such interpreter"):
        server_handoff.hand_off(listener, ["serve"], popen)
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_inherited_takes_the_socket_and_forgets_it():
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        environ = {server_handoff.LISTEN_FD: str(os.dup(held.fileno()))}
        taken = server_handoff.inherited(environ)
        try:
            assert taken.getsockname() == held.getsockname()
            assert environ == {}
        finally:
            taken.close()


def test_no_inherited_socket_without_the_setting():
    assert server_handoff.inherited({}) is None


def test_announce_without_a_payload_writes_only_the_ready_mark():
    read, write = os.pipe()
    os.set_blocking(read, False)
    server_handoff.announce(environ={server_handoff.READY_FD: str(write)})
    assert os.read(read, 64) == server_handoff.READY
    os.close(read)


def test_announce_writes_ready_with_its_payload_once_and_forgets_the_pipe():
    read, write = os.pipe()
    environ = {server_handoff.READY_FD: str(write)}
    os.set_blocking(read, False)
    server_handoff.announce(b"held", environ)
    assert os.read(read, 64) == server_handoff.READY + b"held"
    assert os.read(read, 8) == b""
    assert environ == {}
    os.close(read)
    server_handoff.announce(b"held", environ)
