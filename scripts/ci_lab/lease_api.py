"""The lease API the scheduler serves: the only interface a lab worker calls, and the control routes a swarm calls on
their own port with the lab's bearer token to mark heads hotfix, pause a class but its excepted heads and resume."""

import base64
import hmac
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol, TypeGuard
from urllib.parse import parse_qs, urlsplit

from scripts.ci_lab import control
from scripts.ci_lab.delivery import CLASSES
from scripts.ci_lab.leases import Grant, Leases

__all__ = [
    "MAX_WAIT_MS",
    "STALE",
    "Answer",
    "Bundler",
    "ControlRoutes",
    "Reply",
    "UploadSigner",
    "answer",
    "answer_control",
    "bind",
    "command",
    "respond",
    "serve",
    "serve_control",
    "unbundled",
]

MAX_WAIT_MS = 30_000
STALE = 426
MAX_BODY_BYTES = 65_536
LENGTH = "Content-Length"
AUTHORIZATION = "Authorization"
UPLOADS = frozenset({"report", "log"})

Reply = tuple[int, dict[str, Any]]
Answer = Callable[[str, str, bytes, str | None], Reply]
Bundler = Callable[[str], bytes | None]


def unbundled(sha: str) -> bytes | None:
    """The bundler port of a lease API served without one: no served repository's store holds any commit."""
    return None


class UploadSigner(Protocol):
    def upload_url(self, job: str, name: str) -> str: ...


class BadRequest(Exception):
    pass


@dataclass(frozen=True)
class ControlRoutes:
    """The control routes' lab Redis client, the bearer token they require and the millisecond clock."""

    client: Any
    token: str
    clock: Callable[[], int]


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
        "base": grant.base,
        "grader": grant.grader,
        "kind": grant.unit.kind,
        "key": grant.unit.key,
        "members": list(grant.unit.members),
        "slots": grant.slots,
        "expires_ms": grant.expires_ms,
        **({"manifest": grant.unit.manifest} if grant.unit.manifest else {}),
        **({"manifest_digest": grant.unit.manifest_digest} if grant.unit.manifest_digest else {}),
        **({"graded_commit": grant.graded_commit} if grant.graded_commit else {}),
    }


def _lease(leases: Leases, signer: UploadSigner, query: str, raw: bytes) -> Reply:
    data = _body(raw)
    worker = _text(data, "worker")
    free_slots = _number(data, "free_slots")
    capabilities = _texts(data, "capabilities")
    wait_ms = min(_number(data, "wait_ms"), MAX_WAIT_MS)
    if leases.stale(worker):
        return STALE, {"error": "stale worker"}
    grants = leases.lease(worker, free_slots, capabilities, wait_ms)
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


def _bundle(bundler: Bundler, leases: Leases, signer: UploadSigner, query: str, raw: bytes) -> Reply:
    data = _body(raw)
    job = _text(data, "job")
    token = _text(data, "token")
    if not leases.owns(job, token):
        return _refused()
    record = leases.queue.job(job)
    if record is None or not record.graded_commit:
        raise BadRequest
    found = bundler(record.graded_commit)
    if found is None:
        return 404, {"error": "no served repository's store holds the graded commit"}
    return 200, {"bundle": base64.b64encode(found).decode()}


ROUTES: dict[tuple[str, str], Callable[[Leases, UploadSigner, str, bytes], Reply]] = {
    ("POST", "/lease"): _lease,
    ("POST", "/heartbeat"): _heartbeat,
    ("POST", "/complete"): _complete,
    ("GET", "/upload-url"): _upload_url,
}
BUNDLE_ROUTE = ("POST", "/bundle")


def respond(
    leases: Leases, signer: UploadSigner, method: str, target: str, raw: bytes, bundler: Bundler = unbundled
) -> Reply:
    """Answer one worker request with its status and JSON body."""
    url = urlsplit(target)
    key = (method, url.path)
    route = partial(_bundle, bundler) if key == BUNDLE_ROUTE else ROUTES.get(key)
    if route is None:
        return 404, {"error": "not found"}
    try:
        return route(leases, signer, url.query, raw)
    except BadRequest:
        return 400, {"error": "bad request"}


def _authorised(routes: ControlRoutes, authorization: str | None) -> bool:
    """Return whether the Authorization header carries the lab's bearer token; a lab without a token and a request
    without the header authorise none."""
    expected = f"Bearer {routes.token}".encode()
    return bool(routes.token) and authorization is not None and hmac.compare_digest(authorization.encode(), expected)


def _priority(routes: ControlRoutes, raw: bytes) -> Reply:
    data = _body(raw)
    if data.get("class") != control.HOTFIX:
        raise BadRequest
    heads = _texts(data, "heads")
    now_ms = routes.clock()
    named = control.prioritize(routes.client, heads, now_ms)
    return 200, {"priority": named, "class": control.HOTFIX, "expires_ms": now_ms + control.PRIORITY_MS}


def _pause(routes: ControlRoutes, raw: bytes) -> Reply:
    data = _body(raw)
    name = _text(data, "class")
    excepted = _texts(data, "except")
    if name not in CLASSES:
        raise BadRequest
    return 200, {"paused": name, "except": control.pause(routes.client, name, excepted)}


def _resume(routes: ControlRoutes, raw: bytes) -> Reply:
    return 200, {"resumed": control.resume(routes.client)}


CONTROL: dict[str, Callable[[ControlRoutes, bytes], Reply]] = {
    "/priority": _priority,
    "/pause": _pause,
    "/resume": _resume,
}


def command(routes: ControlRoutes, method: str, target: str, raw: bytes, authorization: str | None) -> Reply | None:
    """Answer one control request with its status and JSON body, or None when the request is no control request."""
    route = CONTROL.get(urlsplit(target).path) if method == "POST" else None
    if route is None:
        return None
    if not _authorised(routes, authorization):
        return 401, {"error": "control token refused"}
    try:
        return route(routes, raw)
    except BadRequest:
        return 400, {"error": "bad request"}


def answer(
    leases: Leases,
    signer: UploadSigner,
    method: str,
    target: str,
    raw: bytes,
    _authorization: str | None,
    bundler: Bundler = unbundled,
) -> Reply:
    """Answer one lease port request through the lease API; the control routes answer only on the control port."""
    return respond(leases, signer, method, target, raw, bundler)


def answer_control(routes: ControlRoutes, method: str, target: str, raw: bytes, authorization: str | None) -> Reply:
    """Answer one control port request: a control route with its bearer token, any other request not found."""
    return command(routes, method, target, raw, authorization) or (404, {"error": "not found"})


def _log_request(request: BaseHTTPRequestHandler, code: int | str = "-", size: int | str = "-") -> None:
    line = re.sub(r"\?[^ ]*", "", request.requestline)
    request.log_message('"%s" %s %s', line, str(code), str(size))


def bind(host: str, port: int, reply: Answer) -> ThreadingHTTPServer:
    """Bind JSON routes on host and port, each request's method, target, body and Authorization header answered by
    reply; the caller runs serve_forever."""

    def handle(request: BaseHTTPRequestHandler) -> None:
        length = request.headers.get(LENGTH) or "0"
        if length.isascii() and length.isdigit() and int(length) <= MAX_BODY_BYTES:
            raw = request.rfile.read(int(length))
            status, payload = reply(request.command, request.path, raw, request.headers.get(AUTHORIZATION))
        else:
            status, payload = 400, {"error": "bad request"}
        data = json.dumps(payload).encode()
        request.send_response(status)
        request.send_header("Content-Type", "application/json")
        request.send_header("Content-Length", str(len(data)))
        request.end_headers()
        request.wfile.write(data)

    handler = type(
        BaseHTTPRequestHandler.__name__,
        (BaseHTTPRequestHandler,),
        {"do_GET": handle, "do_POST": handle, "log_request": _log_request},
    )
    return ThreadingHTTPServer((host, port), handler)


def serve(
    host: str, port: int, leases: Leases, signer: UploadSigner, bundler: Bundler = unbundled
) -> ThreadingHTTPServer:
    """Bind the lease API on host and port; the caller runs serve_forever."""
    return bind(host, port, partial(answer, leases, signer, bundler=bundler))


def serve_control(host: str, port: int, routes: ControlRoutes) -> ThreadingHTTPServer:
    """Bind the control routes alone on host and port; the caller runs serve_forever."""
    return bind(host, port, partial(answer_control, routes))
