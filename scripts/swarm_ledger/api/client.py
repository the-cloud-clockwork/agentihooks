import io
import json
import urllib.error
import urllib.request
import uuid
from urllib.parse import quote, urlencode

from . import schemas


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
        rows, query = [], {"limit": 100}
        while True:
            reply = self.request(slug, f"{path}?{urlencode(query)}")
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
        fetched = {schemas.target(operation) for operation in operations if not operation.get("expected_revision")}
        try:
            return self.send(slug, operations)
        except urllib.error.HTTPError as exc:
            replay, error = failure(exc)
        if replay.code == 403 and "details" in error:
            return error["details"]
        if replay.code != 409 or error.get("code") != "revision_conflict" or not fetched:
            raise replay
        for operation in operations:
            if schemas.target(operation) in fetched:
                del operation["expected_revision"]
        try:
            return self.send(slug, operations)
        except urllib.error.HTTPError as exc:
            raise failure(exc)[0] from None

    def send(self, slug: str, operations: list) -> dict:
        guards, ops = {}, []
        operation_id = operations[0].setdefault("operation_id", uuid.uuid4().hex)
        for operation in operations:
            path = schemas.target(operation)
            if path not in guards:
                guards[path] = operation.get("expected_revision") or self.request(slug, path)["revision"]
            operation.setdefault("expected_revision", guards[path])
            ops.append(
                {key: value for key, value in operation.items() if key not in ("expected_revision", "operation_id")}
            )
        return self.request(slug, "operations", {"operation_id": operation_id, "ops": ops, "guards": guards})


def failure(exc: urllib.error.HTTPError) -> tuple[urllib.error.HTTPError, dict]:
    body = exc.read()
    try:
        error = json.loads(body)["error"]
    except (ValueError, KeyError, TypeError):
        error = {}
    replay = urllib.error.HTTPError(exc.url, exc.code, exc.msg, exc.hdrs, io.BytesIO(body))
    return replay, error if isinstance(error, dict) else {}
