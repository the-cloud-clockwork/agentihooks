import json
import urllib.error
import urllib.request
import uuid
from urllib.parse import quote, urlencode

from . import resources, schemas


class ResourceClient:
    def __init__(self, base: str, credentials: dict) -> None:
        self.base = base
        self.credentials = credentials

    def request(self, slug: str, path: str, payload: dict | None = None) -> dict:
        request = urllib.request.Request(
            f"{self.base}/api/v1/ledgers/{quote(slug, safe='')}/{path}",
            data=None if payload is None else json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", **self.credentials},
            method="GET" if payload is None else "POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
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
        state = self.request(slug, "metadata")["data"]
        for name in resources.COLLECTIONS:
            state[name] = self.collection(slug, name)
        items = {f"{name}/{row['id']}": row for name in resources.THREADS for row in state.get(name, [])}
        for path, row in items.items():
            for thread in resources.THREADS[path.split("/")[0]]:
                row[thread] = []
        for row in self.collection(slug, "threads"):
            parent, thread = row["path"].rsplit("/", 1)
            items[parent][thread].append(row["entry"])
        state["_meta"]["events"] = self.collection(slug, "events")
        members = self.collection(slug, "members")
        state["_meta"]["members"] = {
            row["id"]: {k: v for k, v in row.items() if k not in ("id", "revision")} for row in members
        }
        import ledger_gate

        state["_meta"]["crew"] = ledger_gate.crew(state["_meta"])
        current = self.request(slug, "metadata")["data"]
        if current["_meta"]["rev"] != state["_meta"]["rev"]:
            from .errors import APIError

            raise APIError(409, "revision_conflict", "Ledger changed while resources were read")
        return state

    def mutate(self, slug: str, operations: list) -> dict:
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
        try:
            return self.request(slug, "operations", {"operation_id": operation_id, "ops": ops, "guards": guards})
        except urllib.error.HTTPError as exc:
            if exc.code != 403:
                raise
            error = json.loads(exc.read())["error"]
            if "details" not in error:
                raise
            return error["details"]
