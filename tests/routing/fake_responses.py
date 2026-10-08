import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

EVENTS = (
    {"type": "response.created", "response": {"id": "resp-fake"}},
    {
        "type": "response.output_item.done",
        "item": {
            "type": "message",
            "role": "assistant",
            "id": "msg-fake",
            "content": [{"type": "output_text", "text": "OK"}],
        },
    },
    {
        "type": "response.completed",
        "response": {
            "id": "resp-fake",
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        },
    },
)


class FakeResponses(ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.requests: list[tuple[str, str, str]] = []

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/v1"

    def __enter__(self):
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.shutdown()
        self.server_close()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _record(self) -> None:
        self.server.requests.append((self.command, self.path, self.headers.get("Authorization", "")))

    def do_GET(self):
        self._record()
        self._send(200, "application/json", b'{"models": [], "data": []}')

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._record()
        if not self.path.endswith("/responses"):
            self._send(404, "application/json", b"{}")
            return
        body = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in EVENTS)
        self._send(200, "text/event-stream", body.encode())

    def _send(self, status: int, kind: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
