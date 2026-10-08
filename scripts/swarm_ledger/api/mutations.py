from types import ModuleType

from scripts.swarm_ledger import ledger_task_duplicates

from . import resources, schemas
from .errors import APIError


class GuardedOperations:
    def __init__(self, payload: dict, budget: object, core: ModuleType, fence=None) -> None:
        self.payload = payload
        self.budget = budget
        self.core = core
        self.fence = fence
        self.checked = False
        self.results = {}
        self.digest = resources.revision({"ops": payload["ops"], "changes": payload.get("changes", [])})

    def changes_for(self, op: dict) -> list:
        return self.payload.get("changes", []) if op["id"] == self.payload.get("id") else []

    def check(self, doc: dict, ctx: object) -> None:
        known = ctx.meta.get("api_operations", {}).get(self.payload["operation_id"])
        if known is not None:
            if known["digest"] != self.digest:
                raise APIError(409, "operation_conflict", "Operation identifier was already used for different content")
            self.results = dict(known["results"])
        pending = [op for op in self.payload["ops"] if op["id"] not in self.results]
        targets = {schemas.target(op) for op in pending if op["op"] != "ack" and not self.changes_for(op)}
        for op in pending:
            targets.update(change["path"].rsplit("/", 1)[0] for change in self.changes_for(op))
        for path in targets:
            expected = self.payload["guards"].get(path)
            if expected is None:
                raise APIError(428, "revision_required", "Every changed resource needs an expected revision")
            actual = resources.resource_revision({**doc, "_meta": ctx.meta}, path)
            if expected != actual:
                raise APIError(409, "revision_conflict", "Resource changed since the expected revision")
        self.checked = True

    def check_controller(self, op: dict) -> None:
        from scripts.swarm.store import SwarmError

        if "controller_epoch" in op:
            try:
                self.fence(op["controller_epoch"])
            except SwarmError as exc:
                raise APIError(409, "stale_controller", str(exc)) from None

    def apply(self, doc: dict, op: dict, ctx: object, apply_op: object) -> bool:
        self.check_controller(op)
        if not self.checked:
            self.check(doc, ctx)
        if op["id"] in self.results:
            return self.results[op["id"]]
        changes = self.changes_for(op)
        accepted = (
            not self.core.apply_changes(doc, changes, ctx) if changes else self.budget.apply(doc, op, ctx, apply_op)
        )
        self.check_controller(op)
        self.results[op["id"]] = bool(accepted)
        receipts = ctx.meta.setdefault("api_operations", {})
        receipts[self.payload["operation_id"]] = {"digest": self.digest, "results": dict(self.results)}
        while len(receipts) > 1000:
            del receipts[next(iter(receipts))]
        ctx.dirty = True
        return accepted


def apply(server: ModuleType, slug: str, principal: str, payload: dict) -> dict:
    doc = server.repository.get_document(slug)
    operations = schemas.check_operations(payload, server.core, tuple(t["id"] for t in doc.get("tasks", [])))
    refusals = [reason for op in operations if (reason := server.authority.refusal(principal, op))]
    if principal and payload.get("changes"):
        raise APIError(403, "forbidden", "Checkbox changes need the operator")
    payload = {**payload, "ops": operations}
    if refusals:
        details = {
            "rejected": [op["id"] for op in operations],
            "_meta": {"warnings": [*doc["_meta"].get("warnings", [])[:10], *refusals]},
        }
        raise APIError(403, "forbidden", "Caller cannot perform this operation as its author", details)
    server.ledger_media.resolve(slug, operations)
    server.ledger_artifacts.resolve(slug, operations)
    screen = ledger_task_duplicates.screen(doc, operations)
    gate = ledger_task_duplicates.Gate(
        screen,
        GuardedOperations(
            payload, server.talk.Budget(slug), server.core, lambda epoch: server.authority.fence(slug, epoch)
        ),
    )
    state, rejected = server.repository.apply_ops(slug, ops=operations, gate=gate)
    server.relay_to_inbox(slug, state)
    server.doctor_phrase(slug, state)
    tasks = {op["item"].split("/")[1] for op in operations if op["op"] == "task_update"}
    rows = [resources.project(row) for row in state.get("tasks", []) if row["id"] in tasks]
    reply = {
        **({"tasks": rows} if tasks else {}),
        "applied": [op["id"] for op in operations if op["id"] not in rejected],
        "rejected": rejected,
        "_meta": {
            "rev": state["_meta"]["rev"],
            "warnings": [
                warning[:1000] for warning in [*state["_meta"].get("warnings", [])[:20], *screen.warnings.values()]
            ],
        },
    }
    return bounded_ack(reply)


def bounded_ack(reply: dict) -> dict:
    if resources.reply_size(reply) > resources.MAX_REPLY and "tasks" in reply:
        fields = ("id", "state", "claimed_by", "issue_url", "pr_url", "done", "out_of_scope")
        reply["tasks"] = [{key: row[key] for key in fields if key in row} for row in reply["tasks"]]
    if resources.reply_size(reply) > resources.MAX_REPLY:
        reply.pop("tasks", None)
        reply["task_rows_omitted"] = True
    return resources.bounded(reply)
