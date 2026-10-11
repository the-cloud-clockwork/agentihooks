"""The controller's ledger in deployed mode: the hosted ledger server holds every task and applies every write."""

import json
import urllib.error

from scripts.swarm.store import RedisStore
from scripts.swarm_ledger import ledger
from scripts.swarm_ledger.api.client import ResourceClient, failure
from scripts.swarm_v2.api.tasks import RECEIPTS, RevisionConflict, spec_revision
from scripts.swarm_v2.auth_context import GrantRefused

RECEIPTS_KEY = "hosted-ledger-receipts"
CHANGED = "the task specification changed; refresh and retry"
REUSED = "the operation ID was already used for different content"


def _unavailable(error: BaseException) -> GrantRefused:
    return GrantRefused("dependency_unavailable", f"the hosted ledger is unavailable: {error}")


def _task_key(op: dict) -> str:
    return "/".join((op.get("item") or op["thread"]).split("/")[:2])


class HostedLedger:
    """Reads and writes tasks through the hosted ledger server with the controller credential; it keeps no folder."""

    def __init__(self, base: str, credential: str, store: RedisStore) -> None:
        self.base, self.credential, self.store = base, credential, store

    def client(self) -> ResourceClient:
        return ResourceClient(self.base, {"X-Controller-Credential": self.credential})

    def fetch(self, slug: str, key: str) -> tuple[dict | None, str | None]:
        try:
            reply = ledger.retried(lambda: self.client().request(slug, key), ledger.REMOTE_READ_ATTEMPTS)
        except (OSError, SystemExit) as error:
            if getattr(error, "code", None) == 404:
                return None, None
            raise _unavailable(error) from None
        return reply["data"], reply["revision"]

    def read(self, slug: str, key: str) -> dict:
        task, _ = self.fetch(slug, key)
        return {"tasks": [] if task is None else [task]}

    def apply_ops(self, slug: str, ops: list, gate) -> tuple[dict, list]:
        rev, rejected = None, []
        for op in ops:
            key = _task_key(op)
            task, revision = self.fetch(slug, key)
            write = _Write(self, slug, key, revision)
            task_id, receipts = key.split("/")[1], self.store.key(slug, RECEIPTS_KEY, key)
            if stored := self.store.redis.get(receipts):
                write.meta = {RECEIPTS: {task_id: json.loads(stored)}}
            gate.apply({"tasks": [] if task is None else [task]}, op, write, write.send)
            if (kept := write.meta.get(RECEIPTS, {}).get(task_id)) is not None:
                self.store.redis.set(receipts, json.dumps(kept))
            rev, rejected = write.rev, rejected + write.rejected
        return {"_meta": {"rev": rev}}, rejected


class _Write:
    def __init__(self, hosted: HostedLedger, slug: str, key: str, revision: str | None) -> None:
        self.hosted, self.slug, self.key, self.revision = hosted, slug, key, revision
        self.meta, self.rev, self.rejected = {}, None, []

    def send(self, doc: dict, op: dict, ctx: object) -> bool:
        pin = {"expected_revision": self.revision} if op["op"] == "task_update" else {}
        try:
            reply = self.hosted.client().mutate(self.slug, [{**op, "operation_id": op["id"], **pin}])
        except urllib.error.HTTPError as error:
            raise self._refusal(error) from None
        except OSError as error:
            raise _unavailable(error) from None
        self.rev = reply["_meta"].get("rev")
        self.rejected = reply["rejected"]
        return op["id"] not in reply["rejected"]

    def _refusal(self, error: urllib.error.HTTPError) -> GrantRefused:
        detail = failure(error)[1]
        if detail.get("code") == "operation_conflict":
            return GrantRefused("operation_conflict", REUSED)
        if error.code == 400:
            return GrantRefused("invalid_request", f"the ledger refused this write: {detail.get('message')}")
        if detail.get("code") != "revision_conflict":
            return _unavailable(error)
        task, _ = self.hosted.fetch(self.slug, self.key)
        return RevisionConflict("revision_conflict", CHANGED, self.key.split("/")[1], spec_revision(task or {}))
