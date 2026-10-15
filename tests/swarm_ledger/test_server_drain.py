import signal
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scripts.swarm import ledger_watchdog
from scripts.swarm_ledger import ledger_server, server_drain
from scripts.swarm_ledger.events import Hub


class Held(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.peer = self.client_address
        self.server.entered.set()
        self.server.release.wait(5)
        self.send_response(200)
        self.send_header("Content-Length", "4")
        self.end_headers()
        self.wfile.write(b"done")

    def log_message(self, fmt, *args):
        pass


def running():
    server = server_drain.DrainingServer(("127.0.0.1", 0), Held)
    server.entered, server.release = threading.Event(), threading.Event()
    serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    serving.start()
    return server, serving


def in_flight(server, answers):
    def fetch():
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/", timeout=5) as response:
                answers.append(response.read())
        except OSError as exc:
            answers.append(exc)

    client = threading.Thread(target=fetch, daemon=True)
    client.start()
    assert server.entered.wait(5)
    return client


def test_a_stopping_server_answers_the_request_in_flight_before_it_lets_go_of_the_port():
    server, serving = running()
    answers = []
    client = in_flight(server, answers)
    server.shutdown()
    serving.join(5)
    drained = []
    drainer = threading.Thread(target=lambda: drained.append(server.drain(Hub(), timeout=5)), daemon=True)
    drainer.start()
    drainer.join(0.2)
    assert drainer.is_alive() and drained == [] and answers == []
    server.release.set()
    drainer.join(5)
    client.join(5)
    assert drained == [True]
    assert answers == [b"done"]
    assert server.peer[0] == "127.0.0.1"
    ThreadingHTTPServer(("127.0.0.1", server.server_address[1]), Held).server_close()


def test_a_request_that_outlives_the_drain_wait_lets_the_server_go_anyway():
    server, serving = running()
    client = in_flight(server, [])
    server.shutdown()
    serving.join(5)
    assert server.drain(Hub(), timeout=0.05) is False
    server.release.set()
    client.join(5)


def test_the_server_counts_every_request_in_flight():
    server, serving = running()
    clients = [in_flight(server, []) for _ in range(2)]
    deadline = time.monotonic() + 5
    while server.active < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.active == 2
    server.release.set()
    for client in clients:
        client.join(5)
    server.shutdown()
    serving.join(5)
    assert server.drain(Hub(), timeout=5) is True and server.active == 0


def test_the_drain_wait_ends_before_the_watchdog_kills_the_server():
    assert server_drain.DRAIN_S < ledger_watchdog.STOP_WAIT_S


def test_the_stop_signal_shuts_the_accept_loop_from_another_thread(monkeypatch):
    installed = {}
    monkeypatch.setattr(server_drain.signal, "signal", lambda signum, handler: installed.setdefault(signum, handler))
    server, serving = running()
    server.stop_on(signal.SIGTERM)
    installed[signal.SIGTERM](signal.SIGTERM, None)
    serving.join(5)
    assert not serving.is_alive()
    server.server_close()


def test_a_stop_signal_before_the_accept_loop_runs_never_holds_the_process_open(monkeypatch):
    installed = {}
    monkeypatch.setattr(server_drain.signal, "signal", lambda signum, handler: installed.setdefault(signum, handler))
    server = server_drain.DrainingServer(("127.0.0.1", 0), Held)
    server.stop_on(signal.SIGTERM)
    before = set(threading.enumerate())
    installed[signal.SIGTERM](signal.SIGTERM, None)
    stopping = [thread for thread in threading.enumerate() if thread not in before]
    serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    serving.start()
    serving.join(5)
    stopped = not serving.is_alive()
    server.shutdown()
    server.server_close()
    assert stopped and stopping and all(thread.daemon for thread in stopping)


def test_a_replacement_stops_the_accept_loop_and_says_so():
    server, serving = running()
    assert server.replacing is False
    server.replace()
    serving.join(5)
    assert not serving.is_alive() and server.replacing is True
    server.server_close()


def test_a_deferred_server_binds_nothing_until_asked():
    server = server_drain.DrainingServer(("127.0.0.1", 0), Held, bind_and_activate=False)
    try:
        assert server.server_address == ("127.0.0.1", 0)
        assert server.socket.getsockname()[1] == 0
    finally:
        server.server_close()


@pytest.mark.parametrize("replacing", [True, False])
def test_serve_drains_and_releases_the_writer_before_it_re_execs(monkeypatch, tmp_path, replacing):
    calls = []

    class Server:
        def __init__(self, address, handler, bind_and_activate):
            self.replacing = replacing
            calls.append(("bind_and_activate", bind_and_activate))

        def server_bind(self):
            calls.append(("bind", self.request_queue_size))

        def server_activate(self):
            calls.append("activate")

        def stop_on(self, signum):
            calls.append(("stop_on", signum))

        def serve_forever(self):
            calls.append("serve")

        def drain(self, hub):
            calls.append(("drain", hub))

        def replace(self):
            pass

    class Lease:
        def __init__(self, folder):
            pass

        def acquire(self, owner):
            return self

        def release(self):
            calls.append("release")

    monkeypatch.setattr(ledger_server, "ThreadingHTTPServer", Server)
    monkeypatch.setattr(ledger_server.ledger_writer, "WriterLease", Lease)
    monkeypatch.setattr(ledger_server.threading, "Thread", lambda **kw: type("T", (), {"start": lambda self: None})())
    monkeypatch.setattr(ledger_server.server_lifetime, "watch", lambda *args: threading.Event())
    monkeypatch.setattr(ledger_server.legacy, "adopt", lambda stored: None)
    monkeypatch.setattr(ledger_server, "PIDFILE", tmp_path / ".server.pid")
    monkeypatch.setattr(ledger_server.server_code, "record", lambda *args: None)
    monkeypatch.setattr(ledger_server, "reexec", lambda: calls.append("reexec"))
    ledger_server.serve()
    assert calls == [
        ("bind_and_activate", False),
        ("bind", 128),
        "activate",
        ("stop_on", signal.SIGTERM),
        "serve",
        ("drain", ledger_server.HUB),
        "release",
    ] + (["reexec"] if replacing else [])
