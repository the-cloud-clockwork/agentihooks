"""The controller's ledger in deployed mode: the hosted ledger server holds every task and applies every write."""

import urllib.error

from scripts.swarm_ledger import ledger
from scripts.swarm_ledger.api.client import ResourceClient, failure
from scripts.swarm_v2.api.tasks import RevisionConflict, spec_revision
from scripts.swarm_v2.auth_context import GrantRefused

CHANGED = "the task specification changed; refresh and retry"
REUSED = "the operation ID was already used for different content"


def _unavailable(error: BaseException) -> GrantRefused:
    return GrantRefused("dependency_unavailable", f"the hosted ledger is unavailable: {error}")


def _task_key(op: dict) -> str:
    return "/".join((op.get("item") or op["thread"]).split("/")[:2])


class HostedLedger:
    """Reads and writes tasks through the hosted ledger server with the controller credential; it keeps no folder."""

    def fetch(self, slug: str, key: str) -> tuple[dict | None, str | None]:
        def read() -> dict:
            return ResourceClient(ledger.base(), ledger.credentials(slug), ledger.REQUEST_TIMEOUT).request(slug, key)

        try:
            reply = ledger.retried(read, ledger.REMOTE_READ_ATTEMPTS)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None, None
            raise _unavailable(error) from None
        except (OSError, SystemExit) as error:
            raise _unavailable(error) from None
        return reply["data"], reply["revision"]

    def read(self, slug: str, key: str) -> dict:
        task, _ = self.fetch(slug, key)
        return {"tasks": [] if task is None else [task]}

    def apply_ops(self, slug: str, ops: list, gate) -> tuple[dict, list]:
        write = _Write(self, slug)
        for op in ops:
            write.key = _task_key(op)
            task, write.revision = self.fetch(slug, write.key)
            gate.apply({"tasks": [] if task is None else [task]}, op, write, write.send)
        return {"_meta": {"rev": write.rev}}, write.rejected


class _Write:
    def __init__(self, hosted: HostedLedger, slug: str) -> None:
        self.hosted, self.slug = hosted, slug
        self.meta, self.rev, self.dirty, self.rejected = {}, None, False, []
        self.key = self.revision = None

    def send(self, doc: dict, op: dict, ctx: object) -> bool:
        pin = {"expected_revision": self.revision} if op["op"] == "task_update" else {}
        try:
            reply = ledger.request(self.slug, [{**op, "operation_id": op["id"], **pin}])
        except urllib.error.HTTPError as error:
            raise self._refusal(error) from None
        except (OSError, SystemExit) as error:
            raise _unavailable(error) from None
        self.rev = reply["_meta"].get("rev")
        self.rejected.extend(reply["rejected"])
        return op["id"] not in reply["rejected"]

    def _refusal(self, error: urllib.error.HTTPError) -> GrantRefused:
        code = failure(error)[1].get("code")
        if code == "operation_conflict":
            return GrantRefused("operation_conflict", REUSED)
        if code != "revision_conflict":
            return _unavailable(error)
        task, _ = self.hosted.fetch(self.slug, self.key)
        return RevisionConflict("revision_conflict", CHANGED, self.key.split("/")[1], spec_revision(task or {}))
