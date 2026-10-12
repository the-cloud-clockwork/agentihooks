import base64
import binascii
import hashlib
import json
import re
import threading
from collections.abc import Callable
from dataclasses import asdict, replace
from functools import partial

from scripts.swarm import lease
from scripts.swarm.store import SwarmError
from scripts.swarm_v2.artifacts.object_store import ConditionalBackend
from scripts.swarm_v2.auth_context import GrantRefused, Registration
from scripts.swarm_v2.authority import TaskAuthority

PATH = "/v2/artifacts/backend"
MAX_BYTES = 16 * 1024 * 1024
MAX_REQUEST = 4 * ((MAX_BYTES + 2) // 3) + 4096
FIELDS = {
    "scope": set(),
    "write": {"key", "data"},
    "size": {"key"},
    "read": {"key", "start", "length"},
    "move": {"key", "target"},
    "remove": {"key"},
    "keys": {"key"},
}
MUTATIONS = {"write", "move", "remove"}
STATUS = {
    "unauthenticated": 401,
    "forbidden_scope": 403,
    "stale_generation": 409,
    "invalid_request": 400,
    "operation_conflict": 409,
}


def _refuse(message: str) -> None:
    raise GrantRefused("forbidden_scope", message)


def _key(key: object, scope: Registration, listing: bool) -> str:
    prefix = f"{scope.swarm_id}/{scope.task_id}/"
    if not isinstance(key, str) or not key.startswith(prefix):
        _refuse("artifact key is outside the registered task")
    parts = key.removesuffix("/").split("/")
    if any(part in ("", ".", "..") or "\\" in part for part in parts):
        _refuse("artifact key is not canonical")
    if not listing and len(parts) < 4:
        _refuse("artifact key names no object")
    return key


def _staging(key: str, scope: Registration) -> None:
    parts = key.split("/")
    if len(parts) != 5 or parts[2:4] != ["staging", scope.execution_id]:
        _refuse("staging key is outside the registered execution")


def _document(data: bytes, scope: Registration) -> None:
    document = json.loads(data)
    if not isinstance(document, dict) or (document.get("generation"), document.get("execution_id")) != (
        scope.generation,
        scope.execution_id,
    ):
        _refuse("artifact document is outside the registered task generation")


def _generation_key(parts: list[str], scope: Registration) -> None:
    generation = parts[-2] if parts[2] == "pointers" else parts[-1].removesuffix(".json")
    if not generation.isascii() or not generation.isdigit() or int(generation) != scope.generation:
        _refuse("artifact key is outside the registered task generation")


def _writable(key: str, data: bytes, scope: Registration) -> None:
    parts = key.split("/")
    category = parts[2]
    if category == "staging":
        _staging(key, scope)
        return
    if category == "objects" and len(parts) == 4:
        if not re.fullmatch(r"[a-f0-9]{64}", parts[-1]) or hashlib.sha256(data).hexdigest() != parts[-1]:
            _refuse("artifact object does not match its content address")
        return
    if category not in {"artifacts", "manifests", "pointers"} or not key.endswith(".json"):
        _refuse("artifact key is not writable")
    if category in {"manifests", "pointers"}:
        _generation_key(parts, scope)
    _document(data, scope)


class ArtifactsAPI:
    def __init__(self, backend: ConditionalBackend, tasks: TaskAuthority) -> None:
        self.backend, self.tasks = backend, tasks
        self.lock = threading.RLock()

    def route(self, method: str, path: str, authorization: str, body: object) -> tuple[int, dict]:
        if (method, path) != ("POST", PATH):
            return 404, {"error_class": "invalid_request", "message": "no such artifact endpoint"}
        if not authorization.startswith("Bearer "):
            return 401, {"error_class": "unauthenticated", "message": "a bearer credential is required"}
        token = authorization.removeprefix("Bearer ")
        try:
            operation = self._operation(body)
            with self.lock:
                scope = self._scope(token, False)
                fence = partial(self._guard, token, scope)
                answer = self._dispatch(operation, body, scope, fence)
                if operation in MUTATIONS:
                    fence()
                return 200, answer
        except GrantRefused as error:
            return STATUS[error.error_class], error.detail()
        except (ValueError, TypeError, binascii.Error):
            return 400, {"error_class": "invalid_request", "message": "invalid artifact request"}

    def _operation(self, body: object) -> str:
        if not isinstance(body, dict) or body.get("operation") not in FIELDS:
            raise GrantRefused("invalid_request", "unknown artifact operation")
        operation = body["operation"]
        if set(body) != FIELDS[operation] | {"operation"}:
            raise GrantRefused("invalid_request", "invalid artifact operation fields")
        return operation

    def _scope(self, token: str, mutation: bool) -> Registration:
        registration = self.tasks.authorize(token)
        claim = self.tasks.current(registration.task_id)
        if claim is None or (claim.execution_id, claim.execution_generation) != (
            registration.execution_id,
            registration.generation,
        ):
            raise GrantRefused("stale_generation", "artifact grant does not hold this task generation")
        if mutation:
            try:
                self.tasks.controller.require()
            except SwarmError:
                raise GrantRefused("stale_generation", "artifact controller lease is no longer active") from None
            if claim.state != "active" or claim.lease_deadline_ms <= lease.now_ms(self.tasks.store):
                raise GrantRefused("stale_generation", "artifact task lease is no longer active")
        return replace(registration, generation=claim.generation)

    def _guard(self, token: str, scope: Registration) -> None:
        if self._scope(token, True) != scope:
            raise GrantRefused("stale_generation", "artifact task generation changed during the request")

    def _dispatch(self, operation: str, body: dict, scope: Registration, fence: Callable[[], None]) -> dict:
        if operation == "scope":
            return {"registration": asdict(scope)}
        key = _key(body["key"], scope, operation == "keys")
        if operation == "size":
            return {"size": self.backend.size(key)}
        if operation == "keys":
            return {"keys": self.backend.keys(key)}
        if operation == "read":
            return {"data": base64.b64encode(self._read(key, body["start"], body["length"])).decode()}
        if operation == "write":
            self._write(key, base64.b64decode(body["data"], validate=True), scope, fence)
        elif operation == "move":
            _staging(key, scope)
            target = _key(body["target"], scope, False)
            data = self._read(key, 0, self.backend.size(key))
            self._write(target, data, scope, fence)
            fence()
            self.backend.remove(key)
        else:
            self._remove(key, scope, fence)
        return {}

    def _read(self, key: str, start: int, length: int) -> bytes:
        if type(start) is not int or type(length) is not int or start < 0 or not 0 <= length <= MAX_BYTES:
            raise GrantRefused("invalid_request", "invalid artifact read range")
        size = self.backend.size(key)
        if size is None or start + length > size:
            raise GrantRefused("invalid_request", "artifact read range is outside the object")
        return self.backend.read(key, start, length)

    def _write(self, key: str, data: bytes, scope: Registration, fence: Callable[[], None]) -> None:
        if len(data) > MAX_BYTES:
            raise GrantRefused("invalid_request", "artifact upload exceeds the size limit")
        _writable(key, data, scope)
        size = self.backend.size(key)
        fence()
        if size is None and self.backend.write_once(key, data):
            return
        size = self.backend.size(key)
        if size is None or self._read(key, 0, size) != data:
            raise GrantRefused("operation_conflict", "committed artifact content is immutable")

    def _remove(self, key: str, scope: Registration, fence: Callable[[], None]) -> None:
        _staging(key, scope)
        fence()
        self.backend.remove(key)
