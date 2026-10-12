"""JSON-RPC client for the Codex app-server, which serves WebSocket over its Unix socket."""

import json
import time
from collections.abc import Callable
from typing import Any

CLIENT_NAME = "agentihooks-codex-session"


class ProtocolError(RuntimeError):
    pass


class Connection:
    def __init__(self, ws: Any, timeout_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.ws = ws
        self.timeout_s = timeout_s
        self.clock = clock
        self.next_id = 1

    def request(self, method: str, params: dict | None = None) -> dict:
        from websockets.exceptions import WebSocketException

        try:
            return self._exchange(method, params or {})
        except WebSocketException as exc:
            raise ProtocolError(f"{method} lost the connection: {type(exc).__name__}") from exc

    def _exchange(self, method: str, params: dict) -> dict:
        rid = self.next_id
        self.next_id += 1
        self.ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}))
        deadline = self.clock() + self.timeout_s
        while (left := deadline - self.clock()) > 0:
            try:
                message = json.loads(self.ws.recv(timeout=left))
            except ValueError as exc:
                raise ProtocolError(f"{method} got a frame that is not JSON") from exc
            if message.get("id") != rid or "method" in message:
                continue
            if "error" in message:
                raise ProtocolError(f"{method} refused: {message['error'].get('message', 'no message')}")
            return message.get("result") or {}
        raise TimeoutError(f"no reply to {method}")

    def notify(self, method: str) -> None:
        self.ws.send(json.dumps({"jsonrpc": "2.0", "method": method}))

    def close(self) -> None:
        self.ws.close()


def connect(socket_path: str, timeout_s: float, opener: Callable | None = None) -> tuple[Connection, dict]:
    from websockets.exceptions import WebSocketException
    from websockets.sync.client import unix_connect

    try:
        ws = (opener or unix_connect)(socket_path, open_timeout=timeout_s, max_size=None)
    except (OSError, WebSocketException) as exc:
        raise ProtocolError(f"cannot open the app-server socket: {type(exc).__name__}") from exc
    connection = Connection(ws, timeout_s)
    try:
        info = connection.request("initialize", {"clientInfo": {"name": CLIENT_NAME, "version": "1"}})
        connection.notify("initialized")
    except (OSError, TimeoutError, ProtocolError):
        connection.close()
        raise
    return connection, info


def hooks(connection: Connection, cwd: str) -> list[dict]:
    entries = connection.request("hooks/list", {"cwds": [cwd]}).get("data") or []
    return [hook for entry in entries for hook in entry.get("hooks") or []]


def trust(connection: Connection, untrusted: list[dict]) -> None:
    edits = [
        {
            "keyPath": f"hooks.state.{json.dumps(hook['key'])}.trusted_hash",
            "value": hook["currentHash"],
            "mergeStrategy": "replace",
        }
        for hook in untrusted
    ]
    connection.request("config/batchWrite", {"edits": edits, "reloadUserConfig": True})


def account_type(connection: Connection) -> str:
    return (connection.request("account/read").get("account") or {}).get("type", "")


def start_thread(connection: Connection, cwd: str) -> dict:
    return connection.request("thread/start", {"cwd": cwd}).get("thread") or {}


def resume_thread(connection: Connection, thread_id: str) -> dict:
    return connection.request("thread/resume", {"threadId": thread_id}).get("thread") or {}


def start_turn(connection: Connection, thread_id: str, text: str) -> str:
    result = connection.request("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": text}]})
    return (result.get("turn") or {}).get("id", "")
