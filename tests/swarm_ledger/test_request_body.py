import io

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
    def __init__(self):
        self.timeouts = []

    def settimeout(self, seconds):
        self.timeouts.append(seconds)


class Silent:
    def read(self, size=-1):
        raise TimeoutError("timed out")


def test_drain_bounds_its_wait_and_restores_a_blocking_socket():
    connection = Connection()
    body = request_body.Body(io.BytesIO(b"abc"), 3)

    assert body.drain(10, connection, 2) is True
    assert connection.timeouts == [2, None]


def test_drain_gives_up_on_a_client_that_stops_sending():
    connection = Connection()
    body = request_body.Body(Silent(), 3)

    assert body.drain(10, connection, 2) is False
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

    def read(self, size=-1):
        self.sizes.append(size)
        return super().read(size)


def test_drain_reads_in_chunks_and_accepts_a_body_at_the_limit():
    size = request_body.CHUNK * 2
    raw = Recording(b"x" * size)
    body = request_body.Body(raw, size)

    assert body.drain(size, Connection(), 2) is True
    assert raw.sizes == [request_body.CHUNK, request_body.CHUNK]


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


def test_drain_fails_on_an_unknown_length():
    body = request_body.Body(io.BytesIO(b"abc"), None)

    assert body.read() == b""
    assert body.drain(10, Connection(), 2) is False


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

    handler.send_response(403)

    assert handler.rfile.left == 0
    assert handler.connection.timeouts == [2, None]
    assert b"Connection: close\r\n" not in b"".join(handler._headers_buffer)
    assert handler.close_connection is False
