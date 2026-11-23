from collections.abc import Callable
from typing import Any, cast
from urllib.parse import unquote

from scripts.swarm_ledger import ledger_blockers, ledger_task_duplicates, ports, request_metrics

from . import receipts, resources, schemas
from .errors import APIError
from .services import LedgerCore, Services

WRITES = ("add", "edit", "add_item", "retext")
CLAIM_KEYS = frozenset({"op", "id", "by", "item", "fields", "if_state", "if_match", "controller_epoch", "worktree"})
CLAIM_FIELDS = frozenset({"state", "claimed_by", "workspace", "branch"})
CLAIM_STATES = ("open", "claimed", "pr")
CLAIMANTS = ("swarm",)
ITEM = ledger_blockers.ITEM_RE
ITEMS = ("phases", "questions", "followups", "tasks")
Doc = dict[str, Any]
Warnings = Callable[[], list[str]]


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


def writer(headers) -> str:
    return unquote(headers.get(schemas.WORKTREE_HEADER, ""))


def _stamp(op, worktree: str):
    if not isinstance(op, dict) or op.get("op") not in WRITES:
        return op
    bare = {key: value for key, value in op.items() if key != "worktree"}
    return {**bare, "worktree": worktree} if worktree else bare


def stamped(payload: dict, worktree: str) -> dict:
    if not isinstance(payload.get("ops"), list):
        return payload
    return {**payload, "ops": [_stamp(op, worktree) for op in payload["ops"]]}


def apply(services: Services, slug: str, principal: str, payload: Doc, worktree: str = "") -> Doc:
    if claim(payload) is not None and hasattr(services.repository, "apply_task_update"):
        reply = apply_claim(services, slug, principal, payload, worktree)
        if reply is not None:
            return reply
    with request_metrics.step("guard_read"):
        doc: Doc = services.repository.current(slug).state  # pyright: ignore[reportUnknownMemberType]
    task_ids = tuple(task["id"] for task in doc.get("tasks", []))
    payload, operations = checked(
        services, (slug, principal, worktree), payload, task_ids, lambda: doc["_meta"].get("warnings", [])
    )
    screen = ledger_task_duplicates.screen(doc, operations)
    with request_metrics.step("write_gate"):
        fence, gate = gated(services, slug, payload, screen)
    state: Doc
    rejected: list[str]
    state, rejected = services.repository.apply_ops(slug, ops=operations, gate=gate, copy_state=False)
    with request_metrics.step("publish"):
        published(services, slug, (fence, rejected), state)
    tasks = {op["item"].split("/")[1] for op in operations if op["op"] == "task_update"}
    rows = [cast(Doc, resources.project(row)) for row in state.get("tasks", []) if row["id"] in tasks]
    return answer(rows if tasks else None, operations, rejected, state["_meta"], screen)


def claim(payload: Doc) -> Doc | None:
    """The one hub claim shaped task update a payload carries, or None."""
    ops: Any = payload.get("ops")
    if payload.get("changes") or not isinstance(ops, list) or len(cast(list[Any], ops)) != 1:
        return None
    op: Any = cast(list[Any], ops)[0]
    if not isinstance(op, dict) or cast(Doc, op).get("op") != "task_update" or set(cast(Doc, op)) - CLAIM_KEYS:
        return None
    shaped = cast(Doc, op)
    fields: Any = shaped.get("fields")
    if not isinstance(fields, dict) or not fields or set(cast(Doc, fields)) - CLAIM_FIELDS:
        return None
    if shaped.get("by") not in CLAIMANTS or not (shaped.get("if_state") or shaped.get("if_match")):
        return None
    return shaped if cast(Doc, fields).get("state", "open") in CLAIM_STATES else None


def apply_claim(services: Services, slug: str, principal: str, payload: Doc, worktree: str) -> Doc | None:
    """The reply to a hub claim written on its task's own rows, or None when it needs the whole write."""
    payload, operations = checked(
        services, (slug, principal, worktree), payload, (), lambda: stored_warnings(services, slug)
    )
    screen = ledger_task_duplicates.screen({}, operations)
    with request_metrics.step("write_gate"):
        fence, gate = gated(services, slug, payload, screen)
    written = services.repository.apply_task_update(slug, operations[0], gate)
    if written is None:
        return None
    with request_metrics.step("publish"):
        published(services, slug, (fence, written.rejected), hook_state(services, slug, written.state))
    rows = [cast(Doc, resources.project(written.task))]
    return answer(rows, operations, written.rejected, written.state["_meta"], screen)


def checked(
    services: Services, caller: tuple[str, str, str], payload: Doc, task_ids: tuple[str, ...], warnings: Warnings
) -> tuple[Doc, list[Doc]]:
    slug, principal, worktree = caller
    operations: list[Doc] = [
        {key: value for key, value in op.items() if key != "worktree"}
        for op in schemas.check_operations(stamped(payload, worktree), services.ledger.core, task_ids)
    ]
    refusals = [reason for op in operations if (reason := services.ledger.authority.refusal(principal, op))]
    if principal and payload.get("changes"):
        raise APIError(403, "forbidden", "Checkbox changes need the operator")
    payload = {**payload, "ops": operations}
    if refusals:
        details = {
            "rejected": [op["id"] for op in operations],
            "_meta": {"warnings": [*warnings()[:10], *refusals]},
        }
        raise APIError(403, "forbidden", "Caller cannot perform this operation as its author", details)
    services.media.resolve_media(slug, operations)
    services.media.resolve_artifacts(slug, operations)
    return payload, operations


def stored_warnings(services: Services, slug: str) -> list[str]:
    stored: Doc = services.repository.read(slug, "_meta.warnings")
    return stored.get("_meta", {}).get("warnings", [])


def gated(services: Services, slug: str, payload: Doc, screen: ledger_task_duplicates.Screen) -> tuple[Any, Any]:
    fence: Any = services.ledger.write_gate(slug)
    gate = ledger_task_duplicates.Gate(
        screen,
        GuardedOperations(
            payload, fence, services.ledger.core, lambda epoch: services.ledger.authority.fence(slug, epoch), slug
        ),
    )
    return fence, gate


def published(services: Services, slug: str, decided: tuple[Any, list[str]], state: Doc) -> None:
    fence, rejected = decided
    fence.publish(rejected)
    services.ledger.relay_to_inbox(slug, state)
    services.swarm.doctor_phrase(slug, state)
    services.ledger.deliver_alerts(slug, state)


def hook_state(services: Services, slug: str, state: Doc) -> Doc:
    """The written part of the ledger plus the rows the relay and alert delivery name since the oldest held
    revision."""
    meta: Doc = state["_meta"]
    held: dict[str, dict[str, list[int]]] = services.repository.held_revisions()
    since = min([meta["rev"], *(rev for slugs in held.values() for rev in slugs.get(slug, []))])
    events: list[Doc] = meta.get("events", [])
    alerts: list[Doc] = state.get("alerts", [])
    named = {
        "/".join(event["target"].split("/")[:2])
        for event in events
        if since <= event.get("rev", -1) and event.get("by") == "operator" and event["target"].startswith("tasks/")
    } | {
        alert["item"]
        for alert in alerts
        if since <= alert.get("rev", -1) and alert.get("state") == "open" and ITEM.match(alert.get("item") or "")
    }
    held_rows = {f"{name}/{row['id']}" for name in ITEMS for row in state.get(name, [])}
    missing = sorted(named - held_rows)
    if not missing:
        return state
    found: Doc = services.repository.read(slug, *missing)
    return {**state, **{name: [*state.get(name, []), *found[name]] for name in ITEMS if found.get(name)}}


def answer(
    rows: list[Doc] | None, operations: list[Doc], rejected: list[str], meta: Doc, screen: ledger_task_duplicates.Screen
) -> Doc:
    warnings: list[str] = [*meta.get("warnings", [])[:20], *screen.warnings.values()]
    reply: Doc = {
        **({"tasks": rows} if rows is not None else {}),
        "applied": [op["id"] for op in operations if op["id"] not in rejected],
        "rejected": rejected,
        "_meta": {"rev": meta["rev"], "warnings": [warning[:1000] for warning in warnings]},
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
