import json
import socket
import socketserver
import ssl
import subprocess
import threading
import time
from unittest.mock import Mock

import pytest

from scripts.hive import auth, server

PUBLIC = "redis://hive.example:6380/2"


@pytest.fixture
def admin():
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


@pytest.fixture
def bounded(admin, monkeypatch):
    monkeypatch.setattr(server, "MAX_HANDLERS", 2)
    monkeypatch.setattr(server, "REQUEST_DEADLINE_S", 1)
    monkeypatch.setattr(server, "REQUEST_TIMEOUT_S", 30)
    httpd = server.make_server(admin, PUBLIC, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def _handlers():
    return sum("process_request_thread" in thread.name for thread in threading.enumerate())


def _trickle(port, dropped):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
        conn.settimeout(0.2)
        started = time.monotonic()
        while time.monotonic() - started < 10:
            try:
                conn.sendall(b"P")
                if conn.recv(1) == b"":
                    break
            except TimeoutError:
                continue
            except OSError:
                break
        dropped.append(time.monotonic() - started)


def _join(port, code):
    body = json.dumps({"code": code}).encode()
    with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
        conn.sendall(b"POST /hive/join HTTP/1.0\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))
        reply = b""
        while chunk := conn.recv(65536):
            reply += chunk
    return int(reply.split()[1])


def test_silent_connections_never_run_more_handlers_than_the_cap(bounded):
    before = _handlers()
    silent = [socket.create_connection(("127.0.0.1", bounded), timeout=5) for _ in range(5)]
    try:
        time.sleep(0.5)
        assert _handlers() - before <= server.MAX_HANDLERS
    finally:
        for conn in silent:
            conn.close()


@pytest.mark.wall_clock
def test_trickling_connections_are_dropped_and_a_real_join_is_served(admin, bounded):
    dropped = []
    slow = [threading.Thread(target=_trickle, args=(bounded, dropped)) for _ in range(server.MAX_HANDLERS)]
    for thread in slow:
        thread.start()
    time.sleep(0.3)

    started = time.monotonic()
    assert _join(bounded, auth.invite(admin, "laptop")) == 200
    assert time.monotonic() - started < server.REQUEST_DEADLINE_S + 2

    for thread in slow:
        thread.join(timeout=10)
    assert len(dropped) == server.MAX_HANDLERS
    assert all(elapsed < server.REQUEST_DEADLINE_S + 2 for elapsed in dropped)


@pytest.mark.wall_clock
def test_a_saturated_server_drops_a_stalled_connection_soon_after_its_deadline(admin, bounded):
    opened = time.monotonic()
    silent = [socket.create_connection(("127.0.0.1", bounded), timeout=5) for _ in range(server.MAX_HANDLERS)]
    time.sleep(0.8)
    waiting = threading.Thread(target=_join, args=(bounded, auth.invite(admin, "laptop")))
    waiting.start()
    try:
        for conn in silent:
            assert conn.recv(1) == b""
        assert time.monotonic() - opened < server.REQUEST_DEADLINE_S + 0.6
    finally:
        waiting.join(timeout=10)
        for conn in silent:
            conn.close()


class _Saturated:
    def __init__(self):
        self.waits = []

    def acquire(self, timeout):
        self.waits.append(timeout)
        return len(self.waits) > 2

    def release(self):
        pass


def test_a_saturated_accept_loop_reaps_well_inside_the_deadline(admin, monkeypatch):
    monkeypatch.setattr(server, "REQUEST_DEADLINE_S", 1)
    monkeypatch.setattr(socketserver.ThreadingMixIn, "process_request", lambda *_: None)
    httpd = server.make_server(admin, PUBLIC, "127.0.0.1", 0)
    try:
        httpd._slots = _Saturated()
        reaps = []
        monkeypatch.setattr(httpd, "reap", lambda: reaps.append(True))
        with socket.socket() as request:
            httpd.process_request(request, ("127.0.0.1", 1))

        assert reaps == [True, True]
        assert all(wait <= server.REQUEST_DEADLINE_S / 5 for wait in httpd._slots.waits)
    finally:
        httpd.server_close()


@pytest.fixture
def cert(tmp_path):
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost",
            "-keyout",
            tmp_path / "key.pem",
            "-out",
            tmp_path / "cert.pem",
        ],
        check=True,
        capture_output=True,
    )
    return str(tmp_path / "cert.pem"), str(tmp_path / "key.pem")


@pytest.mark.wall_clock
def test_silent_tls_handshakes_are_dropped_and_a_real_https_join_is_served(admin, cert, monkeypatch):
    monkeypatch.setattr(server, "MAX_HANDLERS", 2)
    monkeypatch.setattr(server, "REQUEST_DEADLINE_S", 1)
    monkeypatch.setattr(server, "REQUEST_TIMEOUT_S", 30)
    httpd = server.make_server(admin, PUBLIC, "localhost", 0, cert)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    port = httpd.server_address[1]
    try:
        silent = [socket.create_connection(("localhost", port), timeout=5) for _ in range(server.MAX_HANDLERS)]
        time.sleep(0.3)
        body = json.dumps({"code": auth.invite(admin, "laptop")}).encode()
        trusting = ssl.create_default_context(cafile=cert[0])
        started = time.monotonic()
        with socket.create_connection(("localhost", port), timeout=5) as raw:
            with trusting.wrap_socket(raw, server_hostname="localhost") as conn:
                conn.sendall(b"POST /hive/join HTTP/1.0\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))
                reply = b""
                while chunk := conn.recv(65536):
                    reply += chunk
        assert int(reply.split()[1]) == 200
        assert time.monotonic() - started < server.REQUEST_DEADLINE_S + 2
        for conn in silent:
            assert conn.recv(1) == b""
            conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_handler_error_names_the_accepted_client(admin, bounded, monkeypatch, capsys):
    def broken(*_):
        raise RuntimeError("exchange broke")

    monkeypatch.setattr(auth, "exchange", broken)
    body = b'{"code": "any"}'
    with socket.create_connection(("127.0.0.1", bounded), timeout=5) as conn:
        conn.sendall(b"POST /hive/join HTTP/1.0\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))
        while conn.recv(65536):
            pass
    time.sleep(0.3)

    assert "request from ('127.0.0.1'" in capsys.readouterr().err


def _refuse(thread):
    raise RuntimeError("no thread")


def test_a_thread_that_cannot_start_gives_its_slot_back(admin, monkeypatch):
    monkeypatch.setattr(server, "MAX_HANDLERS", 1)
    httpd = server.make_server(admin, PUBLIC, "127.0.0.1", 0)
    try:
        monkeypatch.setattr(threading.Thread, "start", _refuse)
        with socket.socket() as request, pytest.raises(RuntimeError):
            httpd.process_request(request, ("127.0.0.1", 1))
        monkeypatch.undo()

        assert httpd._slots.acquire(blocking=False)
        assert not httpd._admitted
    finally:
        httpd.server_close()


def test_a_served_join_gives_its_slot_back(admin, monkeypatch):
    monkeypatch.setattr(server, "MAX_HANDLERS", 1)
    httpd = server.make_server(admin, PUBLIC, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        assert _join(httpd.server_address[1], auth.invite(admin, "laptop")) == 200
        deadline = time.monotonic() + 5
        while httpd._admitted and time.monotonic() < deadline:
            time.sleep(0.05)

        assert not httpd._admitted
        assert httpd._slots.acquire(blocking=False)
        httpd._slots.release()
    finally:
        httpd.shutdown()
        httpd.server_close()


class _Undupable(socket.socket):
    def dup(self):
        raise OSError("descriptor table full")


def test_a_request_whose_socket_cannot_be_duplicated_is_closed(admin):
    httpd = server.make_server(admin, PUBLIC, "127.0.0.1", 0)
    try:
        request = _Undupable()
        with pytest.raises(OSError, match="descriptor table full"):
            httpd.process_request_thread(request, ("127.0.0.1", 1))

        assert request.fileno() == -1
        assert not httpd._open
    finally:
        httpd.server_close()


def test_reaping_continues_when_an_expired_socket_shutdown_raises(admin, monkeypatch):
    monkeypatch.setattr(server.time, "monotonic", lambda: 10)
    failed = Mock(spec=socket.socket)
    failed.shutdown.side_effect = OSError("socket already closed")
    expired = Mock(spec=socket.socket)
    unexpired = Mock(spec=socket.socket)
    with server.make_server(admin, PUBLIC, "127.0.0.1", 0) as httpd:
        httpd._open = {1: (9, failed), 2: (10, expired), 3: (11, unexpired)}

        httpd.reap()

    failed.shutdown.assert_called_once_with(socket.SHUT_RDWR)
    expired.shutdown.assert_called_once_with(socket.SHUT_RDWR)
    unexpired.shutdown.assert_not_called()


@pytest.fixture
def stalled_redis():
    listener = socket.create_server(("127.0.0.1", 0))
    accepted = []

    def accept():
        while True:
            try:
                accepted.append(listener.accept()[0])
            except OSError:
                return

    threading.Thread(target=accept, daemon=True).start()
    yield f"redis://127.0.0.1:{listener.getsockname()[1]}/0"
    listener.shutdown(socket.SHUT_RDWR)
    listener.close()
    for conn in accepted:
        conn.close()


@pytest.mark.wall_clock
@pytest.mark.parametrize(("path", "field"), [(server.JOIN_PATH, "code"), (server.REFRESH_PATH, "ledger_credential")])
def test_a_request_stalled_inside_redis_answers_and_frees_its_slot_within_the_bound(
    stalled_redis, path, field, monkeypatch
):
    import redis

    monkeypatch.setattr(server, "MAX_HANDLERS", 1)
    monkeypatch.setattr(server, "REDIS_TIMEOUT_S", 1)
    httpd = server.make_server(redis.Redis.from_url(stalled_redis, decode_responses=True), PUBLIC, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    body = json.dumps({field: "any"}).encode()
    try:
        started = time.monotonic()
        with socket.create_connection(("127.0.0.1", httpd.server_address[1]), timeout=5) as conn:
            conn.sendall(b"POST %s HTTP/1.0\r\nContent-Length: %d\r\n\r\n%s" % (path.encode(), len(body), body))
            reply = b""
            while chunk := conn.recv(65536):
                reply += chunk
        while httpd._admitted and time.monotonic() - started < 5:
            time.sleep(0.05)

        assert time.monotonic() - started < server.REDIS_TIMEOUT_S * 2
        assert int(reply.split()[1]) == 503
        assert json.loads(reply.partition(b"\r\n\r\n")[2]) == {"error": server.REDIS_STALLED}
        assert not httpd._admitted
    finally:
        httpd.shutdown()
        httpd.server_close()
