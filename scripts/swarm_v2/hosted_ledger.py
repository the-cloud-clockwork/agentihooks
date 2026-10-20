"""The controller's ledger in deployed mode: the hosted ledger server holds every task and applies every write."""

import json
import time
import urllib.error

from scripts.swarm.store import RedisStore
from scripts.swarm_ledger import ledger
from scripts.swarm_ledger.api import client
from scripts.swarm_v2.api.tasks import RECEIPTS, RevisionConflict, spec_revision
from scripts.swarm_v2.auth_context import GrantRefused

RECEIPTS_KEY = "hosted-ledger-receipts"
RECEIPT_TTL = 86400
RECEIPTS_SCRIPT = """
local now, ttl = tonumber(ARGV[1]), tonumber(ARGV[2])
if redis.call('TYPE', KEYS[1]).ok == 'string' then
    local old = cjson.decode(redis.call('GET', KEYS[1]))
    redis.call('DEL', KEYS[1])
    for id, record in pairs(old.operations) do
        record.generation, record.expires_at = old.generation, now + ttl
        redis.call('HSET', KEYS[1], id, cjson.encode(record))
    end
end
if ARGV[3] ~= '' then redis.call('HSET', KEYS[1], ARGV[3], ARGV[4]) end
local records = redis.call('HGETALL', KEYS[1])
for i = 1, #records, 2 do
    if cjson.decode(records[i + 1]).expires_at <= now then redis.call('HDEL', KEYS[1], records[i]) end
end
redis.call('EXPIRE', KEYS[1], ttl)
"""
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

    def client(self) -> client.ResourceClient:
        return client.ResourceClient(self.base, {"X-Controller-Credential": self.credential})

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
            write.gate = gate
            task_id, receipts = key.split("/")[1], self.store.key(slug, RECEIPTS_KEY, key)
            if hasattr(gate, "scope"):
                self.store.redis.eval(RECEIPTS_SCRIPT, 1, receipts, time.time(), RECEIPT_TTL, "")
                operation_id = gate.request["operation_id"]
                if stored := self.store.redis.hget(receipts, operation_id):
                    record = json.loads(stored)
                    write.meta = {
                        RECEIPTS: {task_id: {"generation": record["generation"], "operations": {operation_id: record}}}
                    }
            gate.apply({"tasks": [] if task is None else [task]}, op, write, write.send)
            rev, rejected = write.rev, rejected + write.rejected
        return {"_meta": {"rev": rev}}, rejected


class _Write:
    def __init__(self, hosted: HostedLedger, slug: str, key: str, revision: str | None) -> None:
        self.hosted, self.slug, self.key, self.revision = hosted, slug, key, revision
        self.meta, self.rev, self.rejected = {}, None, []

    def send(self, doc: dict, op: dict, ctx: object) -> bool:
        pin = {"expected_revision": self.revision} if op["op"] == "task_update" else {}
        try:
            write = {**op, "operation_id": op["id"], **pin}
            if hasattr(self.gate, "scope"):
                write["worker_claim"] = {
                    "task_id": self.gate.scope.task_id,
                    "execution_id": self.gate.scope.execution_id,
                    "execution_generation": self.gate.scope.generation,
                    "task_generation": self.gate.request["task_generation"],
                    "spec_revision": self.gate.expected or spec_revision(doc["tasks"][0]),
                }
                reply = self.hosted.client().request(
                    self.slug,
                    "operations",
                    {"operation_id": op["id"], "ops": [{**op, "worker_claim": write["worker_claim"]}], "guards": {}},
                )
            else:
                reply = self.hosted.client().mutate(self.slug, [write])
        except urllib.error.HTTPError as error:
            raise self._refusal(error) from None
        except OSError as error:
            raise _unavailable(error) from None
        self.rev = reply["_meta"].get("rev")
        self.rejected = reply["rejected"]
        accepted = op["id"] not in reply["rejected"]
        if accepted and hasattr(self.gate, "scope"):
            self.gate.remember(doc["tasks"][0], self)
            self.save_receipt()
            self.receipted = True
        return accepted

    def save_receipt(self) -> None:
        task_id = self.gate.scope.task_id
        operation_id = self.gate.request["operation_id"]
        record = self.meta[RECEIPTS][task_id]["operations"][operation_id]
        now = time.time()
        kept = {**record, "generation": self.gate.request["task_generation"], "expires_at": now + RECEIPT_TTL}
        self.hosted.store.redis.eval(
            RECEIPTS_SCRIPT,
            1,
            self.hosted.store.key(self.slug, RECEIPTS_KEY, self.key),
            now,
            RECEIPT_TTL,
            operation_id,
            json.dumps(kept),
        )

    def _refusal(self, error: urllib.error.HTTPError) -> GrantRefused:
        detail = client.failure(error)[1]
        if detail.get("code") == "stale_generation":
            return GrantRefused("stale_generation", "the task generation is no longer current")
        if detail.get("code") == "operation_conflict":
            return GrantRefused("operation_conflict", REUSED)
        if error.code == 400:
            return GrantRefused("invalid_request", f"the ledger refused this write: {detail.get('message')}")
        if detail.get("code") != "revision_conflict":
            return _unavailable(error)
        self.hosted.store.redis.incr(self.hosted.store.key(self.slug, "ledger-revision-conflicts"))
        task, _ = self.hosted.fetch(self.slug, self.key)
        return RevisionConflict("revision_conflict", CHANGED, self.key.split("/")[1], spec_revision(task or {}))
