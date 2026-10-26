import io
from email.message import Message

import pytest

from scripts.swarm_ledger import request_body


def headers(**fields):
    message = Message()
    for name, value in fields.items():
        message[name.replace("_", "-")] = value
    return message


def test_a_read_stops_at_the_declared_length():
    raw = io.BytesIO(b'{"a": 1}GET / HTTP/1.1')
    body = request_body.Body(raw, 8)

    assert body.read() == b'{"a": 1}'
    assert body.read(10) == b""
    assert raw.read() == b"GET / HTTP/1.1"


def test_drain_reads_what_the_handler_left_up_to_the_limit():
    size = request_body.CHUNK * 3 + 5
    raw = io.BytesIO(b"x" * size + b"next")
    body = request_body.Body(raw, size)
    body.read(7)

    assert body.drain(size) is True
    assert body.left == 0
    assert raw.read() == b"next"


def test_drain_leaves_a_body_over_the_limit_unread():
    raw = io.BytesIO(b"x" * 11)
    body = request_body.Body(raw, 11)

    assert body.drain(10) is False
    assert raw.read() == b"x" * 11


def test_drain_fails_when_the_body_ends_early():
    body = request_body.Body(io.BytesIO(b"abc"), 10)

    assert body.drain(10) is False


def test_drain_fails_on_an_unknown_length():
    body = request_body.Body(io.BytesIO(b"abc"), None)

    assert body.read() == b""
    assert body.drain(10) is False


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
