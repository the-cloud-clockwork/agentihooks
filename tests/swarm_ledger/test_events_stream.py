import io

import pytest

from scripts.swarm_ledger.events import Expired, Hub, stream
from tests.swarm_ledger.bounded import bounded

SLUG = "frames-2026-01-01"


def ledger(rev):
    return {"tasks": [], "_meta": {"rev": rev}}


class Sink(io.BytesIO):
    """A response body that runs on_flush at each flush and breaks the pipe after its last one."""

    def __init__(self, flushes, on_flush=None):
        super().__init__()
        self.flushes, self.on_flush, self.marks = flushes, on_flush, []

    def flush(self):
        self.marks.append(len(self.getvalue()))
        if self.on_flush:
            self.on_flush(len(self.marks))
        if len(self.marks) == self.flushes:
            raise BrokenPipeError("client gone")


class Connection:
    def __init__(self):
        self.timeouts = []

    def settimeout(self, value):
        self.timeouts.append(value)


class Handler:
    def __init__(self, sink, headers=None):
        self.headers = headers or {}
        self.wfile, self.connection = sink, Connection()
        self.sent, self.close_connection = [], False

    def send_response(self, code):
        self.sent.append(code)

    def send_header(self, name, value):
        self.sent.append((name, value))

    def end_headers(self):
        self.sent.append("end")


def test_a_frame_carries_its_cursor_event_and_compact_json():
    assert stream.frame("ledger", {"a": [1, "é"]}, "c1") == 'id: c1\nevent: ledger\ndata: {"a":[1,"é"]}\n\n'.encode()
    assert stream.frame("heartbeat", {}) == b"event: heartbeat\ndata: {}\n\n"


def test_parse_reads_frames_back_and_skips_comments_and_empty_blocks():
    text = (
        stream.frame("snapshot", {"x": 1}, "c0").decode()
        + ": comment\n\n"
        + "\n"
        + "data: [1,\r\ndata: 2]\r\n\r\n"
        + "event:bare\ndata:3\nid:c9\n\n"
        + stream.frame("heartbeat", {}).decode()
    )
    assert list(stream.parse(io.StringIO(text))) == [
        ("snapshot", {"x": 1}, "c0"),
        ("message", [1, 2], None),
        ("bare", 3, "c9"),
        ("heartbeat", {}, None),
    ]


def test_parse_keeps_values_whole_and_resets_between_frames():
    lines = ["data: 0\n", "\n", "event: taX\n", "data: 1 \n", "id: c1:X \n", "\n", "data: 2\n", "\n"]
    assert list(stream.parse(lines)) == [("message", 0, None), ("taX", 1, "c1:X "), ("message", 2, None)]


def test_parse_drops_an_unfinished_frame():
    assert list(stream.parse(["event: ledger\n", "data: {}\n"])) == []


def test_serve_sends_the_snapshot_then_heartbeats_until_the_client_goes(monkeypatch):
    monkeypatch.setattr(stream, "HEARTBEAT_S", 0.01)
    hub = Hub()
    handler = Handler(Sink(3))
    assert bounded(stream.serve, handler, hub, SLUG, lambda: {"ledger": ledger(1)})[0] is None
    snapshot = stream.frame("snapshot", {"ledger": ledger(1)}, Hub.cursor(hub.channels[SLUG], 0))
    heartbeat = stream.frame("heartbeat", {})
    assert handler.wfile.getvalue() == snapshot + heartbeat + heartbeat
    assert handler.wfile.marks == [len(snapshot), len(snapshot + heartbeat), len(snapshot + heartbeat * 2)]
    assert handler.sent == [
        200,
        ("Content-Type", "text/event-stream; charset=utf-8"),
        ("Cache-Control", "no-store"),
        ("X-Accel-Buffering", "no"),
        "end",
    ]
    assert handler.close_connection is True
    assert handler.connection.timeouts == [stream.WRITE_TIMEOUT_S]
    assert hub.watched() == []


def test_serve_replays_from_the_cursor_header_then_streams_only_new_events(monkeypatch):
    monkeypatch.setattr(stream, "HEARTBEAT_S", 0.01)
    hub = Hub()
    hub.open(SLUG, lambda: {"ledger": ledger(1)})
    for rev in (2, 3):
        hub.publish(SLUG, "ledger", ledger(rev))
    channel = hub.channels[SLUG]
    replayed = list(channel.log)[1:]

    def publish_once(flush):
        if flush == 1:
            hub.publish(SLUG, "ledger", ledger(4))

    handler = Handler(Sink(2, publish_once), {"Last-Event-ID": channel.log[0][1]})
    bounded(stream.serve, handler, hub, SLUG, None)
    expected = b"".join(stream.frame(name, data, cursor) for _, cursor, name, data in [*replayed, channel.log[-1]])
    assert handler.wfile.getvalue() == expected
    assert hub.watched() == [SLUG]


def test_serve_raises_expired_before_sending_anything():
    hub = Hub()
    hub.open(SLUG, lambda: {"ledger": ledger(1)})
    handler = Handler(Sink(1), {"Last-Event-ID": "gone.0"})
    with pytest.raises(Expired):
        stream.serve(handler, hub, SLUG, None)
    assert handler.sent == [] and handler.wfile.getvalue() == b""
    assert hub.channels[SLUG].subscribers == 1


def test_serve_stops_when_a_slow_reader_falls_out_of_retention(monkeypatch):
    monkeypatch.setattr(stream, "HEARTBEAT_S", 0.01)
    hub = Hub(retained=1)

    def flood(flush):
        for rev in (2, 3, 4):
            hub.publish(SLUG, "ledger", ledger(rev))

    handler = Handler(Sink(5, flood))
    assert bounded(stream.serve, handler, hub, SLUG, lambda: {"ledger": ledger(1)})[0] is None
    assert len(handler.wfile.marks) == 1
    assert hub.watched() == []
