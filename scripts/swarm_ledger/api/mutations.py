from types import ModuleType

from . import resources, schemas
from .errors import APIError


class GuardedOperations:
    def __init__(self, payload: dict, budget: object, core: ModuleType) -> None:
        self.payload = payload
        self.budget = budget
        self.core = core
        self.checked = False
        self.replayed = set()

    def changes_for(self, op: dict) -> list:
        return self.payload.get("changes", []) if op["id"] == self.payload.get("id") else []

    def digest(self, op: dict) -> str:
        return resources.revision({"operation": op, "changes": self.changes_for(op)})

    def check(self, doc: dict, ctx: object) -> None:
        receipts = ctx.meta.get("api_operations", {})
        for op in self.payload["ops"]:
            known = receipts.get(op["id"])
            if known is not None:
                if known != self.digest(op):
                    raise APIError(
                        409, "operation_conflict", "Operation identifier was already used for different content"
                    )
                self.replayed.add(op["id"])
        pending = [op for op in self.payload["ops"] if op["id"] not in self.replayed]
        targets = {schemas.target(op) for op in pending if not self.changes_for(op)}
        for op in pending:
            targets.update(change["path"].rsplit("/", 1)[0] for change in self.changes_for(op))
        for path in targets:
            expected = self.payload["guards"].get(path)
            if expected is None:
                raise APIError(428, "revision_required", "Every changed resource needs an expected revision")
            actual = resources.revision(resources.value({**doc, "_meta": ctx.meta}, path))
            if expected != actual:
                raise APIError(409, "revision_conflict", "Resource changed since the expected revision")
        self.checked = True

    def apply(self, doc: dict, op: dict, ctx: object, apply_op: object) -> bool:
        if not self.checked:
            self.check(doc, ctx)
        if op["id"] in self.replayed:
            return True
        changes = self.changes_for(op)
        accepted = (
            not self.core.apply_changes(doc, changes, ctx) if changes else self.budget.apply(doc, op, ctx, apply_op)
        )
        if accepted:
            receipts = ctx.meta.setdefault("api_operations", {})
            receipts[op["id"]] = self.digest(op)
            while len(receipts) > 1000:
                del receipts[next(iter(receipts))]
            ctx.dirty = True
        return accepted


def apply(server: ModuleType, slug: str, principal: str, payload: dict) -> dict:
    doc = server.repository.get_document(slug, reconcile=False)
    operations = schemas.check_operations(payload, server.core, tuple(t["id"] for t in doc.get("tasks", [])))
    refusals = [reason for op in operations if (reason := server.authority.refusal(principal, op))]
    if principal and payload.get("changes"):
        raise APIError(403, "forbidden", "Checkbox changes need the operator")
    payload = {**payload, "ops": operations}
    if refusals:
        details = {
            "rejected": [op["id"] for op in operations],
            "_meta": {"warnings": [*doc["_meta"].get("warnings", []), *refusals][:100]},
        }
        raise APIError(403, "forbidden", "Caller cannot perform this operation as its author", details)
    server.ledger_media.resolve(slug, operations)
    server.ledger_artifacts.resolve(slug, operations)
    gate = GuardedOperations(payload, server.talk.Budget(slug), server.core)
    state, rejected = server.repository.apply_ops(slug, ops=operations, gate=gate)
    server.relay_to_inbox(slug, state)
    server.doctor_phrase(slug, state)
    tasks = {op["item"].split("/")[1] for op in operations if op["op"] == "task_update"}
    rows = [resources.project(row) for row in state.get("tasks", []) if row["id"] in tasks]
    return {
        **({"tasks": rows} if tasks else {}),
        "applied": [op["id"] for op in operations if op["id"] not in rejected],
        "rejected": rejected,
        "_meta": {
            "rev": state["_meta"]["rev"],
            "warnings": [warning[:1000] for warning in state["_meta"].get("warnings", [])[:20]],
        },
    }
