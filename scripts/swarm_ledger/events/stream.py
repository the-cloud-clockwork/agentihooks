"""Server sent events over one HTTP response: framing, parsing, and the per connection loop."""

import json
from collections.abc import Callable, Generator, Iterable
from http.server import BaseHTTPRequestHandler
from typing import TYPE_CHECKING, Any

from .hub import Closed, Expired

if TYPE_CHECKING:
    from .hub import Hub

HEARTBEAT_S = 5.0
WRITE_TIMEOUT_S = 30.0
COMPACT = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"))


def frame(name: str, data: object, cursor: str | int | None = None) -> bytes:
    head = f"id: {cursor}\n" if cursor else ""
    body = COMPACT.encode(data)
    return f"{head}event: {name}\ndata: {body}\n\n".encode()


def parse(lines: Iterable[str]) -> Generator[tuple[str, Any, str | None]]:
    """Yield (event, data, id) from an iterable of decoded lines."""
    data: list[str]
    cursor: str | None
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


def serve(handler: BaseHTTPRequestHandler, hub: "Hub", slug: str, load: Callable[[], Any]) -> None:
    """Answer one events request until the client goes away or the hub drains; raises Expired before any byte is
    sent."""
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
            try:
                pending = hub.wait(slug, seq, HEARTBEAT_S)
            except Expired:
                seq, pending = hub.snapshot(slug)
    except (OSError, Closed):
        return None
    finally:
        hub.close(slug)
