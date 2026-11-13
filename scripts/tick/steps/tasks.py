"""The task steps of the tick pass, run at their sites in today's order: task passes, then settle and close space."""

from collections.abc import Callable
from functools import partial
from typing import Any, Unpack, cast

from scripts.swarm import (
    dev_red,
    dispatcher,
    fleet_red,
    master_stall,
    phases,
    prerequisite_wait,
    promotion_watch,
    release_wait,
)
from scripts.swarm.ledger_client import LedgerOffline, LedgerRefused
from scripts.tick import registry, steps, view
from scripts.tick.engine import SLOW
from scripts.tick.ports.records import RevisionConflict
from scripts.tick.step import Context, Domain, Lane, Result, Write
from scripts.tick.steps import (
    AGENTS,
    SWARM,
    TASKS,
    CallStep,
    Declared,
    Left,
    actions_of,
    budgets,
    pass_domains,
)

__all__ = [
    "CLOSE",
    "DRAINED",
    "FLEET_RED",
    "FLEET_RED_LIFTED",
    "GROUPS",
    "PHASE_CHANGED",
    "PHASES",
    "SIZES",
    "STALL",
    "RAISED",
    "RANK",
    "REOPENS",
    "STEPS",
    "TASK_PASSES",
    "WriteStep",
    "appliers",
    "fleet_red_writer",
    "fleet_red_writes",
    "group_writer",
    "held",
    "lifted_writer",
    "phase_writer",
    "phase_writes",
    "raise_held",
    "rank_writer",
    "rank_writes",
    "stall_writer",
    "stall_writes",
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
FLEET_RED = Domain("fleet red")
FLEET_RED_LIFTED = Domain("fleet red lifted")
RANK = Domain("rank")
PHASES = Domain("phases")
STALL = Domain("master stall")
PHASE_CHANGED = "phase changed"
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
        except (Left, LedgerOffline):
            return []
        except (Exception, SystemExit) as exc:
            box[RAISED] = exc
            return []

    return run


def placing(call: Callable[[Any, Get], Any]) -> Callable[[Any, Get], Any]:
    """Wrap ``call`` to run holding the host placement lock, so placements of the swarms on a host never overlap."""

    def run(tick: Any, get: Get) -> Any:
        with tick.PLACING:
            return call(tick, get)

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
    return tick.skip_refused(fleet_red_writes, get("slug"), get("store"), get("rows"), get("now_ms"))


def fleet_red_writes(slug: str, store: Any, rows: dict[str, dict[str, Any]], now_ms: int) -> list[str | Write]:
    """Return the listing saves, a guarded hold write and an action per task fleet red newly holds, and a write
    forgetting lifted holds.

    The step saves, posts and forgets nothing itself.
    """
    found, cleared, saves = fleet_red.holds(slug, store, rows, now_ms)
    writes = [*saves, *(item for hold in found for item in _fleet_red_write(*hold))]
    return [*writes, Write(FLEET_RED_LIFTED, slug, "\n".join(cleared))] if cleared else writes


def _fleet_red_write(task_id: str, url: str, checks: str) -> list[str | Write]:
    """Return the hold write of ``task_id``, guarded on its staying open and unclaimed on ``url``, and its action."""
    return [
        Write(
            FLEET_RED,
            task_id,
            {"url": url, "checks": checks},
            expected=_hold_expected(url),
        ),
        fleet_red.held_action(task_id, url, checks),
    ]


def fleet_red_writer(
    slug: str, store: Any, ledger: Any, read: Callable[[str], dict[str, Any] | None]
) -> Callable[[Write], None]:
    """Return the fleet red applier: it posts a hold notice only while the task is still open, unclaimed and red."""
    return partial(_notice_hold, slug, store, ledger, read)


def _notice_hold(
    slug: str, store: Any, ledger: Any, read: Callable[[str], dict[str, Any] | None], write: Write
) -> None:
    """Post the hold notice of ``write``, dropping as a conflict a write that names no pull request and checks."""
    match write.value:
        case {"url": str(url), "checks": str(checks)}:
            _post_hold(slug, store, ledger, read(write.key), write.key, url, checks)
        case _:
            raise RevisionConflict(f"the hold of task {write.key} names no pull request and checks")


def _hold_expected(url: str) -> dict[str, str]:
    """Return the task fields a fleet red hold on ``url`` expects to still hold when it is applied."""
    return {"state": "open", "pr_url": url, "claimed_by": ""}


def _post_hold(
    slug: str, store: Any, ledger: Any, task: dict[str, Any] | None, task_id: str, url: str, checks: str
) -> None:
    """Post the hold notice of ``task_id`` unless the task moved, a seat claimed it or the notice fails.

    A failed notice forgets its mark and is dropped as a conflict, so the next pass posts it again.
    """
    expected = _hold_expected(url)
    moved = [] if task is None else sorted(key for key, value in expected.items() if task.get(key, "") != value)
    if moved:
        raise RevisionConflict(f"task {task_id} changed {', '.join(moved)} since the step read it")
    if store.claimed(slug, [task_id]):
        raise RevisionConflict(f"task {task_id} was claimed since the step read it")
    try:
        fleet_red.post(slug, store, ledger, task_id, url, checks)
    except Exception as exc:
        store.redis.delete(store.key(slug, f"fleet-red:{task_id}"))
        raise RevisionConflict(f"the hold notice of task {task_id} failed: {type(exc).__name__}") from exc


def lifted_writer(store: Any) -> Callable[[Write], None]:
    """Return the lifted holds applier: it forgets the hold marks a write names, which no other step sets."""
    return partial(_forget_holds, store)


def _forget_holds(store: Any, write: Write) -> None:
    """Delete the hold marks ``write`` names, one per line."""
    store.redis.delete(*str(write.value).splitlines())


def rank_writes(
    tick: Any, slug: str, config: Any, store: Any, ledger: Any, doc: dict[str, Any], now_ms: int
) -> list[str | Write]:
    """Return the rank pass decided from ``doc`` as one write guarded on the saved heads it read, taking no step."""
    ranking = tick.dispatcher.rank_decision(slug, config, store, ledger, doc, now_ms)
    return [Write(RANK, slug, ranking, expected=ranking.head)]


def rank_writer(store: Any) -> Callable[[Write], list[str]]:
    """Return the rank applier: it takes a decided rank pass only while the saved heads it read still hold."""
    return partial(_apply_ranking, store)


def _apply_ranking(store: Any, write: Write) -> list[str]:
    """Take the rank pass of ``write`` and return its actions, dropping it as a conflict once the saved heads moved.

    A pass that lands no heads only proposes ranks, so it takes no guard on the saved heads.
    """
    match write.value:
        case dispatcher.Ranking() as ranking:
            if ranking.landed is not None and store.redis.get(store.key(write.key, dispatcher.HEAD)) != write.expected:
                raise RevisionConflict(
                    f"the claim queue heads of swarm {write.key} changed since the rank pass read them"
                )
            return dispatcher.apply_ranking(write.key, store, ranking)
        case _:
            raise RevisionConflict(f"the rank write of swarm {write.key} carries no rank pass")


def phase_writes(found: list[str | phases.PhaseChange]) -> list[str | Write]:
    """Return a write for each phase change ``found`` names, guarded on the done state it read, and its other lines."""
    return [
        Write(PHASES, item.phase_id, item, expected=not item.done) if isinstance(item, phases.PhaseChange) else item
        for item in found
    ]


def phase_writer(slug: str, ledger: Any, inbox: Any, store: Any, box: Any) -> Callable[[Write], list[str]]:
    """Return the phases applier: it sets a phase checkbox only while the done state the step read still holds."""
    return partial(_apply_phase, slug, ledger, inbox, store, box)


def _apply_phase(slug: str, ledger: Any, inbox: Any, store: Any, box: Any, write: Write) -> list[str]:
    """Mark the pass box and set the checkbox of ``write``, dropping it as a conflict once the phase moved.

    The phase is read from the held view after the pass's latest write; without a held view the write goes through.
    """
    match write.value:
        case phases.PhaseChange() as change:
            held = view.current()
            if held is not None and _done_of(held.read("phases", change.phase_id)) != write.expected:
                raise RevisionConflict(f"phase {change.phase_id} changed its checkbox since the phase pass read it")
            box[PHASE_CHANGED] = True
            try:
                return [phases.apply_change(inbox, store, slug, ledger, change)]
            except LedgerRefused as exc:
                raise RevisionConflict(f"the ledger refused the checkbox of phase {change.phase_id}") from exc
        case _:
            raise RevisionConflict(f"the phase write of {write.key} carries no phase change")


def _done_of(row: object) -> bool:
    """Return the done state of a phase row, false for a row that is missing or carries no done flag."""
    match row:
        case {"done": bool() as done}:
            return done
        case _:
            return False


def stall_writes(found: list[str | master_stall.Stall]) -> list[str | Write]:
    """Return a write for each stall decision ``found`` names, guarded on the master it read, and its other lines."""
    return [
        Write(STALL, item.master.name, item, expected=item.master.name)
        if isinstance(item, master_stall.Stall)
        else item
        for item in found
    ]


def stall_writer(slug: str, store: Any, ledger: Any, now_ms: int) -> Callable[[Write], list[str]]:
    """Return the master stall applier: it takes a stall decision only while its master is still the live master."""
    return partial(_apply_stall, slug, store, ledger, now_ms)


def _apply_stall(slug: str, store: Any, ledger: Any, now_ms: int, write: Write) -> list[str]:
    """Take the stall decision of ``write``, dropping it as a conflict once another master took the seat or none is live."""
    match write.value:
        case master_stall.Stall() as stall:
            if master_stall.live_name(store, slug) != write.expected:
                raise RevisionConflict(f"master {write.expected} is no longer the live master of swarm {slug}")
            return master_stall.apply_stall(slug, store, ledger, stall, now_ms)
        case _:
            raise RevisionConflict(f"the stall write of {write.key} carries no stall")


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
        needs: frozenset[str] = frozenset(),
        reads: frozenset[Domain] = frozenset(),
        lane: Lane = CallStep.lane,
        **declared: Unpack[Declared],
    ) -> None:
        """Take the name, group, lane, budgets, needs, domains, effects and feature of the call step running ``call``."""
        base = CallStep(name, group, call, needs, reads, writes, lane, **declared)
        self.effects = base.effects
        self.feature = base.feature
        self.name = base.name
        self.group = base.group
        self.call = base.call
        self.lane = base.lane
        self.floor_s = base.floor_s
        self.interval_s = base.interval_s
        self.needs = base.needs
        self.reads = base.reads
        self.writes = base.writes

    @property
    def budget_s(self) -> float:
        """Return the step's budget this pass: its live budget, never under its floor."""
        return budgets.floored(self.name, self.floor_s)

    def run(self, ctx: Context) -> Result:
        """Run the call against the pass state, returning its actions and its writes."""
        reader = partial(steps.declared_read, ctx, self.name, steps.declared_keys(self.reads | self.writes))
        found: list[object] = list(self.call(steps.import_module("scripts.swarm.tick"), reader) or [])
        writes = tuple(item for item in found if isinstance(item, Write))
        lines = [item for item in found if not isinstance(item, Write)]
        return Result((Write(actions_of(self.name), self.name, lines), *writes))


def task_writer(slug: str, ledger: Any, read: Callable[[str], dict[str, Any] | None]) -> Callable[[Write], None]:
    """Return the tasks applier: it writes a task's fields only while the fields it expects still hold.

    ``read`` returns the task as the ledger holds it now. A moved field or a refused write raises RevisionConflict.
    """
    return partial(_write_task, slug, ledger, read)


def _write_task(slug: str, ledger: Any, read: Callable[[str], dict[str, Any] | None], write: Write) -> None:
    """Write the task fields of ``write`` unless a field it expects moved or the ledger refuses it.

    The expected fields go to the ledger as its ``if_match`` guard, so the server compares and sets them in one apply.
    """
    expected = cast(dict[str, Any], write.expected or {})
    task = read(write.key)
    moved = [] if task is None else sorted(key for key, value in expected.items() if task.get(key, "") != value)
    if moved:
        raise RevisionConflict(f"task {write.key} changed {', '.join(moved)} since the step read it")
    fields = cast(dict[str, Any], write.value)
    try:
        held = ledger.update_task(slug, write.key, fields, if_match=expected or None)
    except LedgerRefused as exc:
        raise RevisionConflict(f"the ledger refused the write of task {write.key}") from exc
    if changed := sorted(key for key in expected if key in fields and held.get(key, "") != fields[key]):
        raise RevisionConflict(f"task {write.key} changed {', '.join(changed)} on the ledger before the write")


def appliers(state: dict[str, Any]) -> dict[Domain, Callable[[Write], list[str] | None]]:
    """Return the appliers of a tick group site: tasks and groups with a ledger and a swarm, fleet red with a store."""
    slug, ledger = state.get("slug"), state.get("ledger")
    if ledger is None or not slug:
        return {}
    found: dict[Domain, Callable[[Write], list[str] | None]] = {
        SIZES: task_writer(slug, ledger, _fresh_task),
        GROUPS: group_writer(slug, ledger, _fresh_task),
    }
    if (store := state.get("store")) is not None:
        found[FLEET_RED] = fleet_red_writer(slug, store, ledger, _fresh_task)
        found[FLEET_RED_LIFTED] = lifted_writer(store)
        found[fleet_red.LISTINGS] = fleet_red.saver(store)
        found[RANK] = rank_writer(store)
        if (now_ms := state.get("now_ms")) is not None:
            found[STALL] = stall_writer(slug, store, ledger, now_ms)
        if (inbox := state.get("inbox")) is not None and (box := state.get("box")) is not None:
            found[PHASES] = phase_writer(slug, ledger, inbox, store, box)
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
    """Release the group under the lead of ``write`` unless its members moved or the ledger refuses it.

    The expected members go to the ledger as its ``if_match`` guard, so the server compares them before it releases.
    """
    expected = cast(dict[str, Any], write.expected or {})
    task = read(write.key)
    if task is not None and list(task.get("group_members") or []) != expected.get("group_members", []):
        raise RevisionConflict(f"task {write.key} changed group_members since the step read it")
    try:
        ledger.ungroup_tasks(slug, write.key, if_match={"group_members": list(expected.get("group_members", []))})
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


DRAINED_RESUME_NEEDS = frozenset(
    {
        "stopped branch",
        "master move fail over",
        "recover master",
        "automated cleanup",
        "master start watch",
        "master wake",
    }
)
RESUMED = frozenset({"drained resume"})
LISTED = frozenset({"ledger state"})
DOC = pass_domains("doc")
CONFIG_DOC = pass_domains("config", "doc")

STEPS = (
    CallStep(
        "sleeping",
        DRAINED,
        held(_sleeping),
        frozenset({"stopped branch", "automated cleanup"}),
        pass_domains("rows"),
        pass_domains("sleeping"),
    ),
    CallStep(
        "drained resume",
        DRAINED,
        held(_drained_resume),
        DRAINED_RESUME_NEEDS | {"sleeping"},
        pass_domains("config", "rows", "doc", "sleeping"),
        pass_domains("config"),
    ),
    CallStep("dev red reopen", REOPENS, held(_dev_red_reopen), RESUMED, pass_domains("config", "rows")),
    CallStep(
        "prerequisite reopen", REOPENS, held(_prerequisite_reopen), frozenset({"dev red reopen"}), pass_domains("rows")
    ),
    CallStep("promotion refresh", REOPENS, held(_promotion_refresh), RESUMED, CONFIG_DOC, lane=SLOW),
    CallStep("release alert", REOPENS, held(_release_alert), RESUMED, CONFIG_DOC, lane=SLOW),
    WriteStep(
        "fleet red notice",
        REOPENS,
        held(_fleet_red_notice),
        writes=frozenset({FLEET_RED, FLEET_RED_LIFTED, fleet_red.LISTINGS}),
        needs=frozenset({"spawn"}),
        reads=pass_domains("rows"),
        lane=SLOW,
    ),
    CallStep("run window", TASK_PASSES, held(_run_window), RESUMED, CONFIG_DOC),
    CallStep("verification notices", TASK_PASSES, held(_verification_notices), LISTED, DOC),
    WriteStep("size pass", TASK_PASSES, held(_size_pass), writes=frozenset({SIZES}), needs=LISTED, reads=DOC),
    WriteStep(
        "grouping release", TASK_PASSES, held(_grouping_release), writes=frozenset({GROUPS}), needs=LISTED, reads=DOC
    ),
    CallStep("dispatcher group", TASK_PASSES, held(_dispatcher_group), RESUMED, CONFIG_DOC),
    CallStep(
        "settle",
        CLOSE,
        held(_settle),
        frozenset({"spawn"}),
        frozenset({AGENTS, TASKS, SWARM}) | pass_domains("config", "rows", "doc"),
        frozenset({AGENTS, TASKS, SWARM}),
    ),
    CallStep("close space", CLOSE, held(_close_space), RESUMED, pass_domains("config")),
)

for _step in STEPS:
    registry.register(_step)
