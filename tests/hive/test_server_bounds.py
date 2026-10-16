import json
import socket
import threading
import time

import pytest

from scripts.hive import auth, server

pytestmark = pytest.mark.xdist_group("fakeredis")

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
    silent = [socket.create_connection(("127.0.0.1", bounded), timeout=5) for _ in range(5)]
    try:
        time.sleep(0.5)
        assert _handlers() <= server.MAX_HANDLERS
    finally:
        for conn in silent:
            conn.close()


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
