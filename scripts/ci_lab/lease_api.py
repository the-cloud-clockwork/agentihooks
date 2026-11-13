"""The lease API the scheduler serves: the only interface a lab worker calls."""

import json
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol, TypeGuard
from urllib.parse import parse_qs, urlsplit

from scripts.ci_lab.leases import Grant, Leases

__all__ = ["MAX_WAIT_MS", "UploadSigner", "respond", "serve"]

MAX_WAIT_MS = 30_000
MAX_BODY_BYTES = 65_536
LENGTH = "Content-Length"
UPLOADS = frozenset({"report", "log"})

Reply = tuple[int, dict[str, Any]]


class UploadSigner(Protocol):
    def upload_url(self, job: str, name: str) -> str: ...


class BadRequest(Exception):
    pass


def _refused() -> Reply:
    return 403, {"error": "lease token refused"}


def _is_body(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def _is_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _body(raw: bytes) -> dict[str, object]:
    try:
        data: object = json.loads(raw)
    except ValueError as error:
        raise BadRequest from error
    if not _is_body(data):
        raise BadRequest
    return data


def _text(data: dict[str, object], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str):
        raise BadRequest
    return value


def _number(data: dict[str, object], name: str) -> int:
    value = data.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise BadRequest
    return value


def _texts(data: dict[str, object], name: str) -> frozenset[str]:
    value = data.get(name)
    if not _is_list(value):
        raise BadRequest
    texts = [item for item in value if isinstance(item, str)]
    if len(texts) != len(value):
        raise BadRequest
    return frozenset(texts)


def _grant(grant: Grant) -> dict[str, Any]:
    return {
        "job": grant.job,
        "token": grant.token,
        "head": grant.head,
        "kind": grant.unit.kind,
        "key": grant.unit.key,
        "members": list(grant.unit.members),
        "slots": grant.slots,
        "expires_ms": grant.expires_ms,
    }


def _lease(leases: Leases, signer: UploadSigner, query: str, raw: bytes) -> Reply:
    data = _body(raw)
    grants = leases.lease(
        _text(data, "worker"),
        _number(data, "free_slots"),
        _texts(data, "capabilities"),
        min(_number(data, "wait_ms"), MAX_WAIT_MS),
    )
    return 200, {"leases": [_grant(grant) for grant in grants]}


def _heartbeat(leases: Leases, signer: UploadSigner, query: str, raw: bytes) -> Reply:
    data = _body(raw)
    expires_ms = leases.heartbeat(_text(data, "job"), _text(data, "token"))
    return _refused() if expires_ms is None else (200, {"expires_ms": expires_ms})


def _complete(leases: Leases, signer: UploadSigner, query: str, raw: bytes) -> Reply:
    data = _body(raw)
    if not leases.complete(_text(data, "job"), _text(data, "token"), _text(data, "outcome")):
        return _refused()
    return 200, {"completed": True}


def _upload_url(leases: Leases, signer: UploadSigner, query: str, raw: bytes) -> Reply:
    fields = {name: values[0] for name, values in parse_qs(query).items()}
    job = fields.get("job")
    token = fields.get("token")
    if job is None or token is None or not leases.owns(job, token):
        return _refused()
    if fields.get("name") not in UPLOADS:
        return 400, {"error": "upload name must be report or log"}
    return 200, {"url": signer.upload_url(job, fields["name"])}


ROUTES: dict[tuple[str, str], Callable[[Leases, UploadSigner, str, bytes], Reply]] = {
    ("POST", "/lease"): _lease,
    ("POST", "/heartbeat"): _heartbeat,
    ("POST", "/complete"): _complete,
    ("GET", "/upload-url"): _upload_url,
}


def respond(leases: Leases, signer: UploadSigner, method: str, target: str, raw: bytes) -> Reply:
    """Answer one worker request with its status and JSON body."""
    url = urlsplit(target)
    route = ROUTES.get((method, url.path))
    if route is None:
        return 404, {"error": "not found"}
    try:
        return route(leases, signer, url.query, raw)
    except BadRequest:
        return 400, {"error": "bad request"}


def serve(host: str, port: int, leases: Leases, signer: UploadSigner) -> ThreadingHTTPServer:
    """Bind the lease API on host and port; the caller runs serve_forever."""

    def answer(request: BaseHTTPRequestHandler) -> None:
        length = request.headers.get(LENGTH) or "0"
        if length.isascii() and length.isdigit() and int(length) <= MAX_BODY_BYTES:
            status, payload = respond(leases, signer, request.command, request.path, request.rfile.read(int(length)))
        else:
            status, payload = 400, {"error": "bad request"}
        data = json.dumps(payload).encode()
        request.send_response(status)
        request.send_header("Content-Type", "application/json")
        request.send_header("Content-Length", str(len(data)))
        request.end_headers()
        request.wfile.write(data)

    handler = type(BaseHTTPRequestHandler.__name__, (BaseHTTPRequestHandler,), {"do_GET": answer, "do_POST": answer})
    return ThreadingHTTPServer((host, port), handler)
