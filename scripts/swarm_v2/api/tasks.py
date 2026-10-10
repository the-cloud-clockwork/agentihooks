import re
from collections.abc import Mapping

from redis.exceptions import RedisError

from scripts.swarm.store import SwarmError
from scripts.swarm_ledger.api.resources import revision
from scripts.swarm_ledger.repository import LedgerRepository
from scripts.swarm_v2.auth_context import IDENTIFIER, GrantRefused, LaunchAuthority, Registration
from scripts.swarm_v2.authority import TaskAuthority

BEARER = "Bearer "
SPEC_FIELDS = (
    "title",
    "description",
    "lane",
    "kind",
    "contract",
    "phase",
    "depends_on",
    "territory",
    "profile",
    "plan_url",
    "plan_slice",
    "plan_lines",
    "slice",
)
WORKER_STATES = ("claimed", "blocked", "pr")
WORKER_FIELDS = ("state", "issue_url", "pr_url", "branch", "branch_repo")
SHOWN = ("id", *SPEC_FIELDS, *WORKER_FIELDS, "claimed_by")
RECEIPTS = "task_operations"
REVISION = re.compile(r"[0-9a-f]{64}")
TASK = re.compile(r"/v2/tasks/([^/]+)")
PROGRESS = re.compile(r"/v2/tasks/([^/]+)/(?:progress|comments)")
OUTCOMES = re.compile(r"/v2/tasks/([^/]+)/outcomes")
WORKER_OUTCOMES = ("done", "blocked")
CONTROLS = re.compile(r"/v2/swarm(/.*)?")
STATUS = {
    "invalid_request": 400,
    "unauthenticated": 401,
    "forbidden_scope": 403,
    "stale_generation": 409,
    "revision_conflict": 409,
    "operation_conflict": 409,
    "dependency_unavailable": 503,
}
CLAIM_REFUSALS = {
    "stale_generation": ("stale_generation", "the task generation is no longer current"),
    "forbidden_scope": ("forbidden_scope", "the worker credential is outside its registered execution"),
    "invalid_request": ("invalid_request", "task_generation must be a positive integer"),
}


def spec_revision(task: Mapping) -> str:
    return revision({name: task.get(name) for name in SPEC_FIELDS})


def ledger_revision_conflicts_total(store, slug: str) -> int:
    return int(store.redis.get(store.key(slug, "ledger-revision-conflicts")) or 0)


class TasksAPI:
    """Worker task endpoints; the ledger repository stays the single writer of task state."""

    def __init__(
        self, grants: LaunchAuthority, tasks: TaskAuthority, ledger: LedgerRepository, writes_enabled: bool = True
    ) -> None:
        self.grants, self.tasks, self.ledger = grants, tasks, ledger
        self.store, self.slug = tasks.store, tasks.slug
        self.writes_enabled = writes_enabled

    def route(self, method: str, path: str, authorization: str, body: object) -> tuple[int, dict]:
        if not authorization.startswith(BEARER):
            return 401, GrantRefused("unauthenticated", "a bearer credential is required").detail()
        token = authorization.removeprefix(BEARER)
        try:
            if CONTROLS.fullmatch(path):
                self.grants.bound(self.slug, token)
                raise GrantRefused("forbidden_scope", "swarm controls need the operator")
            if (subject := TASK.fullmatch(path)) and method == "GET":
                return 200, self.read(token, subject[1])
            if subject and method == "PATCH":
                return 200, self.update(token, subject[1], body)
            if (subject := PROGRESS.fullmatch(path)) and method == "POST":
                return 200, self.progress(token, subject[1], body)
            if (subject := OUTCOMES.fullmatch(path)) and method == "POST":
                return 200, self.propose(token, subject[1], body)
        except GrantRefused as error:
            if isinstance(body, Mapping):
                error.operation_id = _operation_id(body.get("operation_id"))
            return STATUS[error.error_class], error.detail()
        except RedisError:
            return 503, GrantRefused("dependency_unavailable", "the task store is unavailable").detail()
        return 404, GrantRefused("invalid_request", "no such task endpoint").detail()

    def read(self, token: str, task_id: str) -> dict:
        self._scope(token, task_id)
        task = self._task(task_id)
        return {
            "task_id": task_id,
            "revision": spec_revision(task),
            "task": {name: task[name] for name in SHOWN if name in task},
        }

    def update(self, token: str, task_id: str, body: object) -> dict:
        scope = self._writable(token, task_id)
        request = _request(body, ("operation_id", "task_generation", "expected_revision", "fields"))
        _expected_revision(request)
        fields = request["fields"]
        if not isinstance(fields, Mapping) or not fields:
            raise GrantRefused("invalid_request", "fields must name at least one worker field")
        if outside := sorted(set(fields) - set(WORKER_FIELDS)):
            raise GrantRefused("forbidden_scope", f"only the operator changes {', '.join(outside)}")
        if fields.get("state", "claimed") not in WORKER_STATES:
            raise GrantRefused("forbidden_scope", "a worker sets state only to claimed, blocked or pr")
        op = {"op": "task_update", "item": f"tasks/{task_id}", "fields": dict(fields)}
        return self._write(scope, request, op, request["expected_revision"])

    def progress(self, token: str, task_id: str, body: object) -> dict:
        scope = self._writable(token, task_id)
        request = _request(body, ("operation_id", "task_generation", "text"))
        text = request["text"]
        if not isinstance(text, str) or not text.strip():
            raise GrantRefused("invalid_request", "text must be a non empty string")
        op = {"op": "add", "thread": f"tasks/{task_id}/comments", "text": text}
        return self._write(scope, request, op, None)

    def propose(self, token: str, task_id: str, body: object) -> dict:
        scope = self._writable(token, task_id)
        request = _request(body, ("operation_id", "task_generation", "expected_revision", "outcome", "proof"))
        _expected_revision(request)
        if request["outcome"] not in WORKER_OUTCOMES:
            raise GrantRefused("invalid_request", "outcome must be done or blocked")
        proof = request["proof"]
        if not isinstance(proof, str) or not proof.strip():
            raise GrantRefused("invalid_request", "proof must be a non empty string")
        text = f"Outcome proposal: {request['outcome']}. {proof}"
        op = {"op": "add", "thread": f"tasks/{task_id}/comments", "text": text}
        return self._write(scope, request, op, request["expected_revision"])

    def _scope(self, token: str, task_id: str) -> Registration:
        registration = self.grants.bound(self.slug, token)
        if task_id != registration.task_id:
            raise GrantRefused("forbidden_scope", "the credential is scoped to another task")
        return registration

    def _writable(self, token: str, task_id: str) -> Registration:
        scope = self._scope(token, task_id)
        if not self.writes_enabled:
            raise ReadOnly(task_id, spec_revision(self._task(task_id)))
        return scope

    def _task(self, task_id: str) -> dict:
        task = self.ledger.read(self.slug, f"tasks/{task_id}").get("tasks")
        if not task:
            raise GrantRefused("invalid_request", "the task is not on the ledger")
        return task[0]

    def _write(self, scope: Registration, request: dict, op: dict, expected: str | None) -> dict:
        gate = WorkerGate(self, scope, request, op, expected)
        self.ledger.apply_ops(self.slug, ops=[gate.op], gate=gate)
        return gate.ack

    def _hold(self, scope: Registration, generation: object) -> str:
        try:
            claim = self.tasks.current(scope.task_id)
            self.tasks._holder(scope, generation, claim)
        except SwarmError as error:
            error_class, message = CLAIM_REFUSALS.get(
                str(error), ("dependency_unavailable", "the task authority could not confirm the claim")
            )
            raise GrantRefused(error_class, message) from error
        return claim.holder

    def _conflict(self, task: Mapping) -> GrantRefused:
        self.store.redis.incr(self.store.key(self.slug, "ledger-revision-conflicts"))
        return RevisionConflict(
            "revision_conflict", "the task specification changed; refresh and retry", task["id"], spec_revision(task)
        )


class RevisionConflict(GrantRefused):
    def __init__(self, error_class: str, message: str, task_id: str, current: str) -> None:
        super().__init__(error_class, message)
        self.task_id, self.current = task_id, current

    def detail(self) -> dict:
        return {**super().detail(), "task_id": self.task_id, "current_revision": self.current}


class ReadOnly(RevisionConflict):
    def __init__(self, task_id: str, current: str) -> None:
        super().__init__("revision_conflict", "task writes are read only; refresh and retry later", task_id, current)

    def detail(self) -> dict:
        return {**super().detail(), "read_only": True}


class WorkerGate:
    def __init__(self, api: TasksAPI, scope: Registration, request: dict, op: dict, expected: str | None) -> None:
        self.api, self.scope, self.request, self.expected = api, scope, request, expected
        self.digest = revision([op, expected])
        self.op = {**op, "id": f"w-{revision([api.slug, scope.task_id, request['operation_id']])[:16]}"}

    def apply(self, doc: dict, op: dict, ctx: object, apply_op) -> bool:
        generation = self.request["task_generation"]
        holder = self.api._hold(self.scope, generation)
        receipts = ctx.meta.setdefault(RECEIPTS, {}).get(self.scope.task_id)
        if receipts is None or receipts["generation"] != generation:
            receipts = {"generation": generation, "operations": {}}
        known = receipts["operations"].get(self.request["operation_id"])
        if known is not None:
            if known["digest"] != self.digest:
                raise GrantRefused("operation_conflict", "the operation ID was already used for different content")
            self.ack = known["ack"]
            return True
        task = next((row for row in doc["tasks"] if row["id"] == self.scope.task_id), None)
        if task is None:
            raise GrantRefused("invalid_request", "the task is not on the ledger")
        if self.expected is not None and spec_revision(task) != self.expected:
            raise self.api._conflict(task)
        import ledger_core

        write = {**op, "by": holder}
        try:
            ledger_core.check_op(write)
        except ValueError as error:
            raise GrantRefused("invalid_request", f"the ledger refused this write: {error}") from error
        if not apply_op(doc, write, ctx):
            raise GrantRefused("invalid_request", "the ledger refused this write")
        self.api._hold(self.scope, generation)
        self.ack = {
            "task_id": self.scope.task_id,
            "operation_id": self.request["operation_id"],
            "revision": spec_revision(task),
            "ledger_revision": ctx.rev,
        }
        receipts["operations"][self.request["operation_id"]] = {"digest": self.digest, "ack": self.ack}
        ctx.meta[RECEIPTS][self.scope.task_id] = receipts
        ctx.dirty = True
        return True


def _request(body: object, names: tuple[str, ...]) -> dict:
    if not isinstance(body, Mapping) or set(body) != set(names):
        raise GrantRefused("invalid_request", f"the request carries exactly {', '.join(names)}")
    if not isinstance(body["operation_id"], str) or not IDENTIFIER.fullmatch(body["operation_id"]):
        raise GrantRefused("invalid_request", "operation_id must be an identifier")
    return dict(body)


def _expected_revision(request: dict) -> None:
    if not isinstance(request["expected_revision"], str) or not REVISION.fullmatch(request["expected_revision"]):
        raise GrantRefused("invalid_request", "expected_revision must be a task revision from a read")


def _operation_id(value: object) -> str:
    return value if isinstance(value, str) and IDENTIFIER.fullmatch(value) else "unknown"
