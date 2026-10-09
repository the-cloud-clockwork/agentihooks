import io
import itertools
import json
import random
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import quote, urlencode

from . import resources, schemas

RETRIES = 5
BACKOFF = 0.25


class ResourceClient:
    def __init__(self, base: str, credentials: dict, timeout: float = 10) -> None:
        self.base = base
        self.credentials = credentials
        self.timeout = timeout

    def request(self, slug: str, path: str, payload: dict | None = None) -> dict:
        request = urllib.request.Request(
            f"{self.base}/api/v1/ledgers/{quote(slug, safe='')}/{path}",
            data=None if payload is None else json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", **self.credentials},
            method="GET" if payload is None else "POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read())

    def collection(self, slug: str, path: str) -> list:
        rows, query, snapshot = [], {"limit": 100}, None
        swarm = path.startswith("swarm/")
        read = resources.swarm_read if swarm else resources.read
        while True:
            try:
                reply = (
                    self.request(slug, f"{path}?{urlencode(query)}")
                    if snapshot is None
                    else read(snapshot, path, query)
                )
            except urllib.error.HTTPError as exc:
                replay, error = failure(exc)
                if replay.code != 409 or error.get("code") != "revision_conflict":
                    raise replay from None
                snapshot = self.request(slug, "swarm/export" if swarm else "export", {})["data"]
                rows, query = [], {"limit": 100}
                continue
            rows.extend(reply["data"])
            if reply["next_cursor"] is None:
                return rows
            query["cursor"] = reply["next_cursor"]

    def snapshot(self, slug: str) -> dict:
        state = self.request(slug, "export", {})["data"]
        import ledger_gate

        state["_meta"]["crew"] = ledger_gate.crew(state["_meta"])
        return state

    def mutate(self, slug: str, operations: list) -> dict:
        unpinned = [operation for operation in operations if not operation.get("expected_revision")]
        pinned = {schemas.target(operation) for operation in operations if operation.get("expected_revision")}
        fetched = {schemas.target(operation) for operation in unpinned} - pinned
        for attempt in itertools.count():
            try:
                return self.send(slug, operations)
            except urllib.error.HTTPError as exc:
                replay, error = failure(exc)
            if replay.code == 403 and "details" in error:
                return error["details"]
            if not retryable(replay, error, fetched):
                raise replay
            if attempt == RETRIES:
                raise exhausted(replay, error, fetched)
            for operation in unpinned:
                operation.pop("expected_revision", None)
            time.sleep(random.uniform(BACKOFF * 2**attempt / 2, BACKOFF * 2**attempt))

    def send(self, slug: str, operations: list) -> dict:
        pins = [operation for operation in reversed(operations) if operation.get("expected_revision")]
        guards, ops = {schemas.target(operation): operation["expected_revision"] for operation in pins}, []
        operation_id = operations[0].setdefault("operation_id", uuid.uuid4().hex)
        for operation in operations:
            path = schemas.target(operation)
            if path not in guards:
                guards[path] = self.request(slug, path)["revision"]
            operation.setdefault("expected_revision", guards[path])
            ops.append(
                {key: value for key, value in operation.items() if key not in ("expected_revision", "operation_id")}
            )
        return self.request(slug, "operations", {"operation_id": operation_id, "ops": ops, "guards": guards})


def failure(exc: urllib.error.HTTPError) -> tuple[urllib.error.HTTPError, dict]:
    body = exc.read()
    replay = urllib.error.HTTPError(exc.url, exc.code, exc.msg, exc.hdrs, io.BytesIO(body))
    try:
        error = json.loads(body)["error"]
    except (ValueError, KeyError, TypeError):
        return replay, {}
    return replay, error if isinstance(error, dict) else {}


def retryable(replay: urllib.error.HTTPError, error: dict, fetched: set) -> bool:
    if replay.code != 409 or error.get("code") != "revision_conflict":
        return False
    path = (error.get("details") or {}).get("path")
    return path in fetched if path else bool(fetched)


def exhausted(replay: urllib.error.HTTPError, error: dict, fetched: set) -> urllib.error.HTTPError:
    where = (error.get("details") or {}).get("path") or ", ".join(sorted(fetched))
    message = f"revision conflict on {where} persisted after {RETRIES} retries"
    detail = ": ".join(part for part in (error.get("message"), message) if part)
    body = json.dumps({"error": {**error, "message": detail}}).encode()
    return urllib.error.HTTPError(replay.url, replay.code, message, replay.hdrs, io.BytesIO(body))
