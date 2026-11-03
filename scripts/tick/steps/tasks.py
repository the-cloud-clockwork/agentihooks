"""The task steps of the tick pass, run at their sites in today's order: task passes, then settle and close space."""

from collections.abc import Callable
from functools import partial
from typing import Any, cast

from scripts.swarm import dev_red, fleet_red, prerequisite_wait, promotion_watch, release_wait
from scripts.swarm.ledger_client import LedgerRefused
from scripts.tick import registry, steps, view
from scripts.tick.ports.records import RevisionConflict
from scripts.tick.step import Context, Domain, Result, Write
from scripts.tick.steps import AGENTS, PASS, SWARM, TASKS, CallStep, Left, actions_of

__all__ = [
    "CLOSE",
    "DRAINED",
    "GROUPS",
    "SIZES",
    "RAISED",
    "REOPENS",
    "STEPS",
    "TASK_PASSES",
    "WriteStep",
    "appliers",
    "group_writer",
    "held",
    "raise_held",
    "release_writes",
    "size_writes",
    "sleeping_of",
    "task_writer",
]

Get = Callable[[str], Any]
DRAINED = "drained resume"
REOPENS = "reopen passes"
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
        except Left:
            return []
        except (Exception, SystemExit) as exc:
            box[RAISED] = exc
            return []

    return run


def raise_held(box: dict[str, Any]) -> None:
    """Raise the exception a step held, as the hand written pass let it escape."""
    if RAISED in box:
        raise box[RAISED]


def sleeping_of(box: dict[str, Any]) -> bool:
    """Return whether the sleeping step found the swarm asleep, asleep when that step left no answer."""
    return box.get("sleeping", True)


def _sleeping(tick: Any, get: Get) -> Any:
    get("box")["sleeping"] = tick.lifetime.sleeping(get("slug"), get("store"), get("rows"))
    return []


def _drained_resume(tick: Any, get: Get) -> Any:
    slug, store, rows, doc, config = get("slug"), get("store"), get("rows"), get("doc"), get("config")
    if sleeping_of(get("box")) or config.state != "drained":
        return []
    if not any(tick._claimable(slug, store, rows, doc, lane) for lane in tick.LANES):
        return []
    get("box")["config"] = store.update(slug, state="running")
    return ["new tasks, running again"]


def _dev_red_reopen(tick: Any, get: Get) -> Any:
    keys = ("slug", "config", "store", "ledger", "rows")
    return tick.skip_refused(dev_red.reopen_pass, *map(get, keys))  # pyright: ignore[reportUnknownMemberType]


def _prerequisite_reopen(tick: Any, get: Get) -> Any:
    keys = ("slug", "store", "ledger", "rows")
    return tick.skip_refused(prerequisite_wait.reopen_pass, *map(get, keys))  # pyright: ignore[reportUnknownMemberType]


def _promotion_refresh(tick: Any, get: Get) -> Any:
    keys = ("slug", "config", "store", "ledger", "doc", "now_ms")
    return tick.skip_refused(promotion_watch.refresh, *map(get, keys))  # pyright: ignore[reportUnknownMemberType]


def _release_alert(tick: Any, get: Get) -> Any:
    keys = ("slug", "config", "ledger", "doc")
    return tick.skip_refused(release_wait.alert_pass, *map(get, keys))  # pyright: ignore[reportUnknownMemberType]


def _fleet_red_notice(tick: Any, get: Get) -> Any:
    keys = ("slug", "store", "rows", "now_ms")
    return tick.skip_refused(fleet_red.notice_pass, *map(get, keys))


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
    return [item for found in tick.difficulty.sizes(doc) for item in _size_write(tick, *found)]


def _size_write(tick: Any, task_id: str, fields: dict[str, Any]) -> list[str | Write]:
    """Return the guarded task write of the size ``fields`` give task ``task_id``, and its action."""
    return [Write(SIZES, task_id, fields, expected={"difficulty": ""}), tick.difficulty.sized_action(task_id, fields)]


class WriteStep:
    """A task step whose call returns action lines and the task writes the engine applies with compare and set."""

    def __init__(
        self,
        name: str,
        group: str,
        call: Callable[[Any, Get], Any],
        writes: frozenset[Domain] = frozenset(),
    ) -> None:
        """Take the name, group, budgets and domains of the call step running ``call``."""
        base = CallStep(name, group, call, writes=writes)
        self.name = base.name
        self.group = base.group
        self.call = base.call
        self.lane = base.lane
        self.budget_s = base.budget_s
        self.interval_s = base.interval_s
        self.needs = base.needs
        self.reads = base.reads
        self.writes = base.writes

    def run(self, ctx: Context) -> Result:
        """Run the call against the pass state, returning its actions and its writes."""
        found = cast(
            list[object], self.call(steps.import_module("scripts.swarm.tick"), partial(ctx.view.read, PASS)) or []
        )
        writes = tuple(item for item in found if isinstance(item, Write))
        lines = [item for item in found if not isinstance(item, Write)]
        return Result((Write(actions_of(self.name), self.name, lines), *writes))


def task_writer(slug: str, ledger: Any, read: Callable[[str], dict[str, Any] | None]) -> Callable[[Write], None]:
    """Return the tasks applier: it writes a task's fields only while the fields it expects still hold.

    ``read`` returns the task as the ledger holds it now. A moved field or a refused write raises RevisionConflict.
    """
    return partial(_write_task, slug, ledger, read)


def _write_task(slug: str, ledger: Any, read: Callable[[str], dict[str, Any] | None], write: Write) -> None:
    """Write the task fields of ``write`` unless a field it expects moved or the ledger refuses it."""
    expected = cast(dict[str, Any], write.expected or {})
    task = read(write.key)
    moved = [] if task is None else sorted(key for key, value in expected.items() if task.get(key, "") != value)
    if moved:
        raise RevisionConflict(f"task {write.key} changed {', '.join(moved)} since the step read it")
    try:
        ledger.update_task(slug, write.key, write.value)
    except LedgerRefused as exc:
        raise RevisionConflict(f"the ledger refused the write of task {write.key}") from exc


def appliers(state: dict[str, Any]) -> dict[Domain, Callable[[Write], None]]:
    """Return the appliers of a task group site: tasks, groups and fleet red holds when the site holds a ledger and a
    swarm, and fleet red listings and releases when it holds a store."""
    slug, ledger, store = state.get("slug"), state.get("ledger"), state.get("store")
    found: dict[Domain, Callable[[Write], None]] = {}
    if ledger is not None and slug:
        found = {SIZES: task_writer(slug, ledger, _fresh_task), GROUPS: group_writer(slug, ledger, _fresh_task)}
        if store is not None:
            found[fleet_red.HOLDS] = fleet_red.holder(store, ledger, slug)
    if store is not None:
        found |= {fleet_red.LISTINGS: fleet_red.saver(store), fleet_red.RELEASES: fleet_red.releaser(store)}
    return found


def _fresh_task(task_id: str) -> dict[str, Any] | None:
    """Return the task as the held ledger view has it after the pass's latest write, empty when the view lacks it.

    Without a held view there is nothing to compare against, so the result is ``None`` and the write goes through.
    """
    held = view.current()
    return None if held is None else cast(dict[str, Any], held.read("tasks", task_id) or {})


def _grouping_release(tick: Any, get: Get) -> Any:
    return tick.skip_refused(release_writes, tick, get("slug"), get("store"), get("doc"))


def release_writes(tick: Any, slug: str, store: Any, doc: dict[str, Any]) -> list[str | Write]:
    """Return a group write and an action for each stopped group lead, writing nothing and leaving ``doc`` as read."""
    return [item for found in tick.grouping.releases(slug, store, doc) for item in _release_write(tick, doc, *found)]


def _release_write(tick: Any, doc: dict[str, Any], lead_id: str, why: str, released: list[str]) -> list[str | Write]:
    """Return the guarded group write and the action releasing ``released`` from the group under ``lead_id``."""
    lead = next(task for task in doc["tasks"] if task["id"] == lead_id)
    return [
        Write(GROUPS, lead_id, released, expected={"group_members": list(lead["group_members"])}),
        tick.grouping.released_action(lead_id, why, released),
    ]


def group_writer(slug: str, ledger: Any, read: Callable[[str], dict[str, Any] | None]) -> Callable[[Write], None]:
    """Return the groups applier: it releases a lead's group only while the lead still holds the members it read."""
    return partial(_release_group, slug, ledger, read)


def _release_group(slug: str, ledger: Any, read: Callable[[str], dict[str, Any] | None], write: Write) -> None:
    """Release the group under the lead of ``write`` unless its members moved or the ledger refuses it."""
    expected = cast(dict[str, Any], write.expected or {})
    task = read(write.key)
    if task is not None and list(task.get("group_members") or []) != expected.get("group_members", []):
        raise RevisionConflict(f"task {write.key} changed group_members since the step read it")
    try:
        ledger.ungroup_tasks(slug, write.key)
    except LedgerRefused as exc:
        raise RevisionConflict(f"the ledger refused the release of the group under task {write.key}") from exc


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
    CallStep("sleeping", DRAINED, held(_sleeping)),
    CallStep("drained resume", DRAINED, held(_drained_resume), frozenset({"sleeping"})),
    CallStep("dev red reopen", REOPENS, held(_dev_red_reopen)),
    CallStep("prerequisite reopen", REOPENS, held(_prerequisite_reopen), frozenset({"dev red reopen"})),
    CallStep("promotion refresh", REOPENS, held(_promotion_refresh)),
    CallStep("release alert", REOPENS, held(_release_alert)),
    WriteStep("fleet red notice", REOPENS, held(_fleet_red_notice), writes=fleet_red.DOMAINS),
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
