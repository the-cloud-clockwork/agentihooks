# pyright: reportUnusedFunction=false, reportUnusedClass=false
"""Claim order, agent status and the tick's launch records.

Moved code reaches swarm tick module names through ``tick`` so patched tick seams apply.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any, Protocol

from scripts.swarm import readiness
from scripts.swarm.pane import PaneObservation
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig


class SpawnError(RuntimeError):
    def __init__(self, message: str, status: str = "refused", pane_id: str = "", *, harness: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.pane_id = pane_id
        self.harness = harness

    def step_fields(self) -> dict[str, str]:
        """The message and status a failed tick step records for this launch."""
        return {"detail": str(self), "status": str(self.status)}

    def waiting(self) -> bool:
        """Whether this launch only waits: its hive acknowledgement is pending or its account reservation is full."""
        full = self.status == "unavailable" and str(self).startswith("account reservation is full")
        return self.status == "pending" or full


@dataclass(frozen=True)
class Placed:
    pane_id: str
    harness: str
    account: str = ""
    model: str = ""
    effort: str = ""
    placement: str = ""
    profile: str = ""
    model_source: str = ""
    model_confidence: float | None = None
    profile_decision: dict = field(default_factory=dict)  # pyright: ignore[reportMissingTypeArgument, reportUnknownVariableType]
    choice: str = ""
    launched_at: int = 0
    overlays: list = field(default_factory=list)  # pyright: ignore[reportMissingTypeArgument, reportUnknownVariableType]
    launch_timings: dict = field(default_factory=dict)  # pyright: ignore[reportMissingTypeArgument, reportUnknownVariableType]
    route_kind: str = "subscription"
    transport: str = ""
    execution: AgentRecord | None = None
    remote_control: dict[str, Any] = field(default_factory=dict)
    desired_route_kind: str | None = None
    conversation_id: str = ""


class Ledger(Protocol):
    def state(self, slug: str) -> dict[str, Any]: ...
    def update_task(
        self,
        slug: str,
        task_id: str,
        fields: dict[str, Any],
        by: str = ...,
        if_state: tuple[Any, ...] = ...,
        if_match: dict[str, Any] | None = ...,
    ) -> dict[str, Any]: ...
    def comment(self, slug: str, task_id: str, text: str, by: str) -> None: ...
    def notice(self, slug: str, task_id: str, text: str) -> None: ...
    def notify(self, slug: str, text: str) -> None: ...
    def closed(self, slug: str) -> bool: ...
    def bin_closed(self, slug: str, closed_at: int) -> bool: ...


class Runtime(Protocol):
    def has_capacity(self, config: Any) -> bool: ...
    def spawn(self, config: Any, lane: str, name: str, task: dict[str, Any]) -> Placed: ...
    def live_names(self) -> set[str]: ...
    def reported(self, agent: AgentRecord) -> bool: ...
    def bindings(self, agents: list[AgentRecord]) -> dict[str, Any]: ...
    def recover(self, name: str) -> Placed: ...
    def retire(self, agent: AgentRecord, homes: list[Any] = ...) -> bool: ...
    def refusal(self, agent: AgentRecord) -> dict[str, Any]: ...
    def reap_name(self, name: str) -> bool: ...
    def status(self, agent: AgentRecord) -> str: ...
    def observe(self, agent: AgentRecord) -> PaneObservation: ...
    def nudge(self, agent: AgentRecord, text: str) -> str | None: ...
    def dismiss(self, agent: AgentRecord) -> bool: ...
    def name_pane(self, agent: AgentRecord) -> bool: ...
    def conversations(self) -> dict[str, str] | None: ...
    def resume(self, config: Any, agent: AgentRecord, text: str) -> Placed: ...
    def close_space(self, config: SwarmConfig) -> bool: ...
    def pane_tail(self, pane_id: str, lines: int) -> str: ...
    def launched_pane(self, pane_id: str, name: str) -> bool: ...
    def close_pane(self, pane_id: str) -> None: ...


def agent_status(agent: Any) -> Any:
    if agent.state in {"finished", "awaiting-decision"}:
        return agent.state
    if agent.input_prompt:
        return "waiting"
    if agent.idle_ticks >= tick.IDLE_NUDGE_TICKS:
        return "stalled"
    return "idle" if agent.idle_ticks else "working"


def ready_in_order(
    slug: Any, store: Any, rows: Any, doc: Any, lane: Any, dispatched: Any = None, claims: Any = None
) -> Any:
    return tick._launch_order(
        slug, store, tick._claimable(slug, store, rows, doc, lane, dispatched, claims), dispatched
    )


def ready_by_lane(
    slug: str,
    store: RedisStore,
    rows: dict[str, dict[str, Any]],
    doc: dict[str, Any],
    lanes: Iterable[str],
    dispatched: Callable[[dict[str, Any]], tuple[Any, ...]] | None = None,
    claims: "Claims | None" = None,
) -> dict[str, list[dict[str, Any]]]:
    """Each lane's claimable tasks in launch order, from one claim view and one launch order for the pass."""
    claims = claims or tick.claim_view(slug, store, rows, doc, dispatched)
    found = {lane: tick._claimable(slug, store, rows, doc, lane, dispatched, claims) for lane in lanes}
    ordered = tick._launch_order(slug, store, [task for tasks in found.values() for task in tasks], dispatched)
    return {lane: [task for task in ordered if task.get("lane") == lane] for lane in found}


def _owned_tasks(slug: Any, store: Any) -> Any:
    return {agent.task for agent in store.agents(slug) if agent.state != "finished"}


def _claimable(
    slug: Any, store: Any, rows: Any, doc: Any, lane: Any, dispatched: Any = None, claims: Any = None
) -> Any:
    claims = claims or tick.claim_view(slug, store, rows, doc, dispatched)
    held = list(claims.held)
    clear, overlapping = [], []
    for t in claims.ordered:
        if tick.readiness.claimable(t, lane, claims.view):
            mine = t.get("territory") or []  # pyright: ignore[reportUnknownVariableType]
            if any(tick._overlaps(mine, other) for other in held):
                overlapping.append(t)  # pyright: ignore[reportUnknownMemberType]
                continue
            clear.append(t)  # pyright: ignore[reportUnknownMemberType]
            held.append(mine)
    return clear + overlapping  # pyright: ignore[reportUnknownVariableType]


@dataclass(frozen=True)
class Claims:
    view: readiness.Snapshot
    held: list[Any]
    ordered: list[Any]


def claim_view(
    slug: str,
    store: RedisStore,
    rows: dict[str, dict[str, Any]],
    doc: dict[str, Any],
    dispatched: Callable[[dict[str, Any]], tuple[Any, ...]] | None = None,
) -> Claims:
    awaiting = tick._owned_tasks(slug, store)
    claimed = store.claimed(slug, [task_id for task_id, t in rows.items() if t.get("state") == "open"])
    held = [t.get("territory") or [] for t in rows.values() if t.get("state") in tick.ACTIVE]  # pyright: ignore[reportUnknownVariableType]
    config = store.config(slug)
    graph, fix = tick.hierarchy.project(doc)[0], tick.freeze.fix_phase(config)
    released, applied = tick.release_wait.checker(config), tick.apply_wait.checker(config, rows)
    unshared = tick.fleet_red.checker(tick.fleet_red.saved_reader(store, slug))
    view = tick.readiness.Snapshot(
        slug,
        rows,
        doc,
        graph,
        fix,
        lambda task_id: task_id in awaiting or task_id in claimed,  # pyright: ignore[reportUnknownLambdaType]
        lambda task: released(task) and applied(task) and unshared(task),  # pyright: ignore[reportUnknownLambdaType]
    )
    return tick.Claims(view, held, sorted(rows.values(), key=dispatched or tick.claim_order.key(rows)))


def _launch_order(slug: Any, store: Any, tasks: Any, dispatched: Any = None) -> Any:
    demoted = tick._demoted(slug, store, [task["id"] for task in tasks])
    return sorted(
        tasks,
        key=lambda task: (
            dispatched(task)[0] if dispatched else tick.ledger_rank.order(task),
            task["id"] in demoted,
            not tick.claim_order.resumed(task),
        ),
    )


def _demoted(slug: Any, store: Any, task_ids: Any) -> Any:
    failed = store.launch_failures(slug)
    held = [task_id for task_id in task_ids if failed.get(task_id)]
    return set(held) - store.handoff_tasks(slug, held) if held else set()  # pyright: ignore[reportUnknownVariableType]


tick: Any = import_module("scripts.swarm.tick")
