"""Server sent events over one HTTP response: framing, parsing, and the per connection loop."""

import json

from .hub import Expired

HEARTBEAT_S = 5.0
WRITE_TIMEOUT_S = 30.0


def frame(name, data, cursor=None):
    head = f"id: {cursor}\n" if cursor else ""
    return f"{head}event: {name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()


def parse(lines):
    """Yield (event, data, id) from an iterable of decoded lines."""
    name, data, cursor = "message", [], None
    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line:
            if data:
                yield name, json.loads("\n".join(data)), cursor
            name, data, cursor = "message", [], None
            continue
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "event":
            name = value
        elif field == "data":
            data.append(value)
        elif field == "id":
            cursor = value


def serve(handler, hub, slug, load):
    """Answer one events request until the client goes away; raises Expired before any byte is sent."""
    seq, first = hub.open(slug, load, handler.headers.get("Last-Event-ID"))
    try:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Accel-Buffering", "no")
        handler.end_headers()
        handler.close_connection = True
        handler.connection.settimeout(WRITE_TIMEOUT_S)
        pending = first
        while True:
            for at, cursor, name, data in pending:
                handler.wfile.write(frame(name, data, cursor))
                seq = at
            if not pending:
                handler.wfile.write(frame("heartbeat", {}))
            handler.wfile.flush()
            pending = hub.wait(slug, seq, HEARTBEAT_S)
    except (Expired, OSError):
        return None
    finally:
        hub.close(slug)
