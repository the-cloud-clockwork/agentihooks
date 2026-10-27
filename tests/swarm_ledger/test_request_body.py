import ast
import io
import socket
from pathlib import Path

import pytest

from scripts.swarm_ledger import ledger_server, request_body


def headers(**fields):
    return {name.replace("_", "-"): value for name, value in fields.items()}


def test_a_read_stops_at_the_declared_length():
    raw = io.BytesIO(b'{"a": 1}GET / HTTP/1.1')
    body = request_body.Body(raw, 8)

    assert body.read() == b'{"a": 1}'
    assert body.read(10) == b""
    assert raw.read() == b"GET / HTTP/1.1"


def test_a_sized_read_returns_at_most_that_many_bytes():
    raw = io.BytesIO(b"abcdefgh")
    body = request_body.Body(raw, 8)

    assert body.read(0) == b""
    assert body.read(3) == b"abc"
    assert body.left == 5
    assert body.read(None) == b"defgh"


class Connection:
    def __init__(self, received=()):
        self.timeouts, self.calls, self.received = [], [], list(received)

    def settimeout(self, seconds):
        self.timeouts.append(seconds)

    def shutdown(self, how):
        self.calls.append(("shutdown", how))

    def recv(self, size):
        self.calls.append(("recv", size))
        return self.received.pop(0) if self.received else b""


class Clock:
    def __init__(self, *readings):
        self.readings = list(readings)

    def __call__(self):
        return self.readings.pop(0)


class Silent:
    def read1(self, size=-1):
        raise TimeoutError("timed out")


class Trickle(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.sizes = []

    def read1(self, size=-1):
        self.sizes.append(size)
        return super().read1(1)


def test_drain_waits_only_what_is_left_of_its_deadline_and_restores_a_blocking_socket():
    connection = Connection()
    body = request_body.Body(io.BytesIO(b"abc"), 3)

    assert body.drain(10, connection, 2, clock=Clock(0, 0.25)) is True
    assert connection.timeouts == [1.75, None]


def test_drain_stops_a_trickling_client_at_its_deadline():
    connection = Connection()
    body = request_body.Body(Trickle(b"x" * 10), 10)

    assert body.drain(10, connection, 2, clock=Clock(0, 0.5, 1.5, 2.5)) is False
    assert connection.timeouts == [1.5, 0.5, None]
    assert body.left == 8


def test_drain_reads_nothing_once_its_deadline_is_reached():
    raw = Trickle(b"x" * 10)
    connection = Connection()

    assert request_body.Body(raw, 10).drain(10, connection, 2, clock=Clock(0, 2)) is False
    assert raw.sizes == []
    assert connection.timeouts == [None]


def test_drain_gives_up_on_a_client_that_stops_sending():
    connection = Connection()
    body = request_body.Body(Silent(), 3)

    assert body.drain(10, connection, 2, clock=Clock(0, 0)) is False
    assert connection.timeouts == [2, None]


def test_drain_waits_on_nothing_it_will_not_read():
    connection = Connection()

    assert request_body.Body(io.BytesIO(b"x" * 11), 11).drain(10, connection, 2) is False
    assert request_body.Body(io.BytesIO(b""), None).drain(10, connection, 2) is False
    assert connection.timeouts == []


class Recording(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.sizes = []

    def read1(self, size=-1):
        self.sizes.append(size)
        return super().read1(size)


def test_drain_reads_in_chunks_and_accepts_a_body_at_the_limit():
    size = 65536 * 2 + 5
    raw = Recording(b"x" * size)
    body = request_body.Body(raw, size)

    assert body.drain(size, Connection(), 2) is True
    assert raw.sizes == [65536, 65536, 5]


def test_drain_reads_what_the_handler_left_up_to_the_limit():
    size = request_body.CHUNK * 3 + 5
    raw = io.BytesIO(b"x" * size + b"next")
    body = request_body.Body(raw, size)
    body.read(7)

    assert body.drain(size, Connection(), 2) is True
    assert body.left == 0
    assert raw.read() == b"next"


def test_drain_leaves_a_body_over_the_limit_unread():
    raw = io.BytesIO(b"x" * 11)
    body = request_body.Body(raw, 11)

    assert body.drain(10, Connection(), 2) is False
    assert raw.read() == b"x" * 11


def test_drain_fails_when_the_body_ends_early():
    body = request_body.Body(io.BytesIO(b"abc"), 10)

    assert body.drain(10, Connection(), 2) is False


def test_a_body_of_unknown_length_reads_as_asked_and_never_drains():
    body = request_body.Body(io.BytesIO(b"abc"), None)

    assert body.read(2) == b"ab"
    assert body.left is None
    assert body.drain(10, Connection(), 2) is False


def test_linger_shuts_the_write_side_and_discards_until_the_client_closes():
    connection = Connection([b"ab", b"cd"])

    request_body.linger(connection, 2, clock=Clock(0, 0.5, 1.0, 1.5))

    assert connection.calls == [("shutdown", socket.SHUT_WR)] + [("recv", 65536)] * 3
    assert connection.timeouts == [1.5, 1.0, 0.5]


def test_linger_stops_at_its_deadline():
    connection = Connection([b"x"] * 5)

    request_body.linger(connection, 2, clock=Clock(0, 1, 2))

    assert connection.calls == [("shutdown", socket.SHUT_WR), ("recv", 65536)]
    assert connection.timeouts == [1]


class Reset(Connection):
    def shutdown(self, how):
        raise ConnectionResetError("reset")


def test_linger_ends_quietly_on_a_closed_socket():
    connection = Reset([b"x"])

    assert request_body.linger(connection, 2, clock=Clock(0, 1)) is None
    assert connection.calls == []


def test_close_closes_the_connection_stream():
    raw = io.BytesIO(b"")
    request_body.Body(raw, 0).close()

    assert raw.closed


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({}, 0),
        ({"Content_Length": "12"}, 12),
        ({"Content_Length": "twelve"}, None),
        ({"Content_Length": "-1"}, None),
        ({"Content_Length": "12", "Transfer_Encoding": "chunked"}, None),
    ],
)
def test_length_reads_content_length_and_refuses_what_it_cannot_count(fields, expected):
    assert request_body.length(headers(**fields)) == expected


def test_a_response_over_an_unread_body_closes_the_connection():
    handler = ledger_server.Handler.__new__(ledger_server.Handler)
    handler.request_version, handler.requestline = "HTTP/1.1", "POST /x HTTP/1.1"
    handler.command, handler.path, handler.client_address = "POST", "/x", ("127.0.0.1", 0)
    handler.close_connection = False
    handler.rfile = request_body.Body(io.BytesIO(b""), None)
    handler.connection = Connection()

    handler.send_response(400)

    assert b"Connection: close\r\n" in b"".join(handler._headers_buffer)
    assert handler.close_connection is True


def test_each_parsed_request_reads_its_body_through_a_counted_reader():
    handler = ledger_server.Handler.__new__(ledger_server.Handler)
    handler.raw_rfile = io.BytesIO(b"Content-Length: 2\r\n\r\n{}next")
    handler.rfile = handler.raw_rfile
    handler.raw_requestline = b"POST /x HTTP/1.1\r\n"

    assert handler.parse_request() is True
    assert handler.rfile.raw is handler.raw_rfile
    assert handler.rfile.left == 2
    assert handler.rfile.read() == b"{}"


def test_a_response_drains_an_unread_body_within_its_wait():
    handler = ledger_server.Handler.__new__(ledger_server.Handler)
    handler.request_version, handler.requestline = "HTTP/1.1", "POST /x HTTP/1.1"
    handler.command, handler.path, handler.client_address = "POST", "/x", ("127.0.0.1", 0)
    handler.close_connection = False
    handler.rfile = request_body.Body(io.BytesIO(b"{}"), 2)
    handler.connection = Connection()

    handler.send_response(403, "Refused here")

    assert handler._headers_buffer[0] == b"HTTP/1.0 403 Refused here\r\n"
    assert handler.rfile.left == 0
    wait, restored = handler.connection.timeouts
    assert 1.9 < wait <= 2
    assert restored is None
    assert b"Connection: close\r\n" not in b"".join(handler._headers_buffer)
    assert handler.close_connection is False


def finished(body):
    handler = ledger_server.Handler.__new__(ledger_server.Handler)
    handler.wfile, handler.rfile, handler.connection = io.BytesIO(), body, Connection()
    handler.finish()
    return handler.connection


def test_a_connection_left_holding_body_lingers_before_it_closes():
    connection = finished(request_body.Body(io.BytesIO(b"{}"), 2))

    assert connection.calls == [("shutdown", socket.SHUT_WR), ("recv", 65536)]
    assert 1.9 < connection.timeouts[0] <= 2


def test_a_connection_whose_body_was_read_closes_at_once():
    connection = finished(request_body.Body(io.BytesIO(b""), 0))

    assert connection.calls == []


def test_the_ledger_server_assigns_each_module_constant_once():
    tree = ast.parse(Path(ledger_server.__file__).read_text())
    names = [
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    ]

    assert sorted(name for name in set(names) if names.count(name) > 1) == []


def test_the_server_drain_keeps_its_own_bound_beside_the_body_drain():
    assert (ledger_server.DRAIN_S, ledger_server.BODY_DRAIN_S) == (5.0, 2)
