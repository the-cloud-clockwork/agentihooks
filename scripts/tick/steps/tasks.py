"""The task steps of the tick pass, run at their sites in today's order: task passes, then settle and close space."""

from collections.abc import Callable
from functools import partial
from typing import Any, cast

from scripts.swarm.ledger_client import LedgerRefused
from scripts.tick import registry, steps, view
from scripts.tick.ports.records import RevisionConflict
from scripts.tick.step import Context, Domain, Result, Write
from scripts.tick.steps import AGENTS, PASS, SWARM, TASKS, CallStep, actions_of

__all__ = [
    "CLOSE",
    "GROUPS",
    "SIZES",
    "RAISED",
    "STEPS",
    "TASK_PASSES",
    "WriteStep",
    "appliers",
    "group_writer",
    "held",
    "raise_held",
    "release_writes",
    "size_writes",
    "task_writer",
]

Get = Callable[[str], Any]
GROUPS = Domain("groups")
SIZES = Domain("sizes")
TASK_PASSES = "task passes"
CLOSE = "close"
RAISED = "raised"


def held(call: Callable[[Any, Get], Any]) -> Callable[[Any, Get], Any]:
    """Wrap ``call`` to skip once a step held an exception and to hold the exception it raises."""

    def run(tick: Any, get: Get) -> Any:
        box = get("box")
        if RAISED in box:
            return []
        try:
            return call(tick, get)
        except (Exception, SystemExit) as exc:
            box[RAISED] = exc
            return []

    return run


def raise_held(box: dict[str, Any]) -> None:
    """Raise the exception a step held, as the hand written pass let it escape."""
    if RAISED in box:
        raise box[RAISED]


def _run_window(tick: Any, get: Get) -> Any:
    return tick.skip_refused(
        tick.run_window.step, get("slug"), get("config"), get("store"), get("ledger"), get("doc"), get("now_ms")
    )


def _verification_notices(tick: Any, get: Get) -> Any:
    return tick.skip_refused(tick.claim_hold.verification_notices, get("slug"), get("store"), get("doc"), get("now_ms"))


def _size_pass(tick: Any, get: Get) -> Any:
    return tick.skip_refused(size_writes, tick, get("doc"))


def size_writes(tick: Any, doc: dict[str, Any]) -> list[str | Write]:
    """Return a task write and an action for each task the size pass sizes, writing nothing itself."""
    found: list[str | Write] = []
    for task_id, fields in tick.difficulty.sizes(doc):
        found += [
            Write(SIZES, task_id, fields, expected={"difficulty": ""}),
            tick.difficulty.sized_action(task_id, fields),
        ]
    return found


class WriteStep(CallStep):
    """A task step whose call returns action lines and the task writes the engine applies with compare and set."""

    def run(self, ctx: Context) -> Result:
        """Run the call against the pass state, returning its actions and its writes."""
        found = self.call(steps.import_module("scripts.swarm.tick"), partial(ctx.view.read, PASS)) or []
        writes = tuple(item for item in found if isinstance(item, Write))
        lines = [item for item in found if not isinstance(item, Write)]
        return Result((Write(actions_of(self.name), self.name, lines), *writes))


def task_writer(slug: str, ledger: Any, read: Callable[[str], dict[str, Any] | None]) -> Callable[[Write], None]:
    """Return the tasks applier: it writes a task's fields only while the fields it expects still hold.

    ``read`` returns the task as the ledger holds it now. A moved field or a refused write raises RevisionConflict.
    """

    def apply(write: Write) -> None:
        expected = cast(dict[str, Any], write.expected or {})
        task = read(write.key) or {}
        moved = sorted(key for key, value in expected.items() if task.get(key, "") != value)
        if moved:
            raise RevisionConflict(f"task {write.key} changed {', '.join(moved)} since the step read it")
        try:
            ledger.update_task(slug, write.key, write.value)
        except LedgerRefused as exc:
            raise RevisionConflict(f"the ledger refused the write of task {write.key}") from exc

    return apply


def appliers(state: dict[str, Any]) -> dict[Domain, Callable[[Write], None]]:
    """Return the appliers of a task group site: tasks and groups when the site holds a ledger and a swarm."""
    if state.get("ledger") is None or not state.get("slug"):
        return {}
    slug, ledger = cast(str, state["slug"]), state["ledger"]
    return {SIZES: task_writer(slug, ledger, _fresh_task), GROUPS: group_writer(slug, ledger, _fresh_task)}


def _fresh_task(task_id: str) -> dict[str, Any] | None:
    """Return the task as the held ledger view has it after the pass's latest write, or nothing without a view."""
    held = view.current()
    return None if held is None else cast(dict[str, Any] | None, held.read("tasks", task_id))


def _grouping_release(tick: Any, get: Get) -> Any:
    return tick.skip_refused(release_writes, tick, get("slug"), get("store"), get("doc"))


def release_writes(tick: Any, slug: str, store: Any, doc: dict[str, Any]) -> list[str | Write]:
    """Return a group write and an action for each stopped group lead, writing nothing and leaving ``doc`` as read."""
    found: list[str | Write] = []
    for lead_id, why, released in tick.grouping.releases(slug, store, doc):
        lead = next(task for task in doc["tasks"] if task["id"] == lead_id)
        members = list(lead["group_members"])
        found += [
            Write(GROUPS, lead_id, released, expected={"group_members": members}),
            tick.grouping.released_action(lead_id, why, released),
        ]
    return found


def group_writer(slug: str, ledger: Any, read: Callable[[str], dict[str, Any] | None]) -> Callable[[Write], None]:
    """Return the groups applier: it releases a lead's group only while the lead still holds the members it read."""

    def apply(write: Write) -> None:
        expected = cast(dict[str, Any], write.expected or {})
        task = read(write.key) or {}
        if list(task.get("group_members") or []) != expected.get("group_members", []):
            raise RevisionConflict(f"task {write.key} changed group_members since the step read it")
        try:
            ledger.ungroup_tasks(slug, write.key)
        except LedgerRefused as exc:
            raise RevisionConflict(f"the ledger refused the release of the group under task {write.key}") from exc

    return apply


def _dispatcher_group(tick: Any, get: Get) -> Any:
    return tick.skip_refused(
        tick.dispatcher.group, get("slug"), get("config"), get("store"), get("ledger"), get("doc"), get("now_ms")
    )


def _settle(tick: Any, get: Get) -> Any:
    return tick.skip_refused(
        tick._settle, get("slug"), get("config"), get("store"), get("ledger"), get("rows"), get("doc")
    )


def _close_space(tick: Any, get: Get) -> Any:
    return tick.skip_refused(tick._close_space, get("slug"), get("config"), get("store"), get("runtime"))


STEPS = (
    CallStep("run window", TASK_PASSES, held(_run_window)),
    CallStep("verification notices", TASK_PASSES, held(_verification_notices)),
    WriteStep("size pass", TASK_PASSES, held(_size_pass), writes=frozenset({SIZES})),
    WriteStep("grouping release", TASK_PASSES, held(_grouping_release), writes=frozenset({GROUPS})),
    CallStep("dispatcher group", TASK_PASSES, held(_dispatcher_group)),
    CallStep(
        "settle", CLOSE, held(_settle), frozenset({"spawn"}), frozenset({AGENTS, TASKS, SWARM}), frozenset({SWARM})
    ),
    CallStep("close space", CLOSE, held(_close_space)),
)

for _step in STEPS:
    registry.register(_step)
