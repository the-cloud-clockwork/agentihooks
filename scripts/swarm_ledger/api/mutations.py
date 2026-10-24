from scripts.swarm_ledger import ledger_task_duplicates, ports

from . import receipts, resources, schemas
from .errors import APIError
from .services import LedgerCore, Services


class GuardedOperations:
    def __init__(self, payload: dict, budget: object, core: LedgerCore, fence=None, slug: str = "") -> None:
        self.payload = payload
        self.budget = budget
        self.core = core
        self.fence = fence
        self.slug = slug
        self.checked = False
        self.results = {}
        self.digest = resources.revision({"ops": payload["ops"], "changes": payload.get("changes", [])})

    def changes_for(self, op: dict) -> list:
        return self.payload.get("changes", []) if op["id"] == self.payload.get("id") else []

    def check(self, doc: dict, ctx: object) -> None:
        _, known = receipts.find(ctx.meta, self.payload["operation_id"])
        if known is not None:
            if known["digest"] != self.digest:
                raise APIError(409, "operation_conflict", "Operation identifier was already used for different content")
            self.results = dict(known["results"])
        pending = [op for op in self.payload["ops"] if op["id"] not in self.results]
        targets = {
            schemas.target(op)
            for op in pending
            if op["op"] not in schemas.UNGUARDED and not self.changes_for(op) and "worker_claim" not in op
        }
        for op in pending:
            targets.update(change["path"].rsplit("/", 1)[0] for change in self.changes_for(op))
        for path in targets:
            expected = self.payload["guards"].get(path)
            if expected is None:
                raise APIError(428, "revision_required", "Every changed resource needs an expected revision")
            actual = resources.resource_revision({**doc, "_meta": ctx.meta}, path)
            if expected != actual:
                raise APIError(409, "revision_conflict", "Resource changed since the expected revision", {"path": path})
        self.checked = True

    def check_controller(self, op: dict) -> None:
        SwarmError = ports.swarm().store.SwarmError

        if "controller_epoch" in op:
            try:
                self.fence(op["controller_epoch"])
            except SwarmError as exc:
                raise APIError(409, "stale_controller", str(exc)) from None

    def apply(self, doc: dict, op: dict, ctx: object, apply_op: object) -> bool:
        self.check_controller(op)
        from . import worker_writes

        worker_writes.check(self.slug, doc, op)
        if not self.checked:
            self.check(doc, ctx)
        if op["id"] in self.results:
            return self.results[op["id"]]
        changes = self.changes_for(op)
        accepted = (
            not self.core.apply_changes(doc, changes, ctx) if changes else self.budget.apply(doc, op, ctx, apply_op)
        )
        self.check_controller(op)
        worker_writes.check(self.slug, doc, op)
        self.results[op["id"]] = bool(accepted)
        receipts.store(ctx.meta, self.payload["operation_id"], self.digest, dict(self.results))
        ctx.dirty = True
        return accepted


def apply(services: Services, slug: str, principal: str, payload: dict) -> dict:
    doc = services.repository.current(slug).state
    operations = schemas.check_operations(payload, services.ledger.core, tuple(t["id"] for t in doc.get("tasks", [])))
    refusals = [reason for op in operations if (reason := services.ledger.authority.refusal(principal, op))]
    if principal and payload.get("changes"):
        raise APIError(403, "forbidden", "Checkbox changes need the operator")
    payload = {**payload, "ops": operations}
    if refusals:
        details = {
            "rejected": [op["id"] for op in operations],
            "_meta": {"warnings": [*doc["_meta"].get("warnings", [])[:10], *refusals]},
        }
        raise APIError(403, "forbidden", "Caller cannot perform this operation as its author", details)
    services.media.resolve_media(slug, operations)
    services.media.resolve_artifacts(slug, operations)
    screen = ledger_task_duplicates.screen(doc, operations)
    fence = services.ledger.write_gate(slug)
    gate = ledger_task_duplicates.Gate(
        screen,
        GuardedOperations(
            payload, fence, services.ledger.core, lambda epoch: services.ledger.authority.fence(slug, epoch), slug
        ),
    )
    state, rejected = services.repository.apply_ops(slug, ops=operations, gate=gate, copy_state=False)
    fence.publish(rejected)
    services.ledger.relay_to_inbox(slug, state)
    services.swarm.doctor_phrase(slug, state)
    services.ledger.deliver_alerts(slug, state)
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
