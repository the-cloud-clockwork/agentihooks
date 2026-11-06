# pyright: reportUnusedFunction=false, reportUnusedClass=false
"""Claim order, territory overlap, the settle and close of a swarm, agent status and the tick's launch records.

Moved code reaches swarm tick module names through ``tick`` so patched tick seams apply.
"""

from collections.abc import Callable
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
    launch_job: dict[str, Any] = field(default_factory=dict)


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


def _parked(task: Any, rows: Any) -> Any:
    return any(rows.get(dep, {}).get("state") != "done" for dep in task.get("parked_on") or [])  # pyright: ignore[reportUnknownVariableType]


def _stack_base(task: Any, rows: Any, repo: Any, origin: Any = None) -> Any:
    deps = [dep for dep in task.get("depends_on") or [] if rows[dep].get("state") != "done"]  # pyright: ignore[reportUnknownVariableType]
    return [tick._stacked_dep(dep, rows[dep], repo, origin or tick.stack.origin_key) for dep in deps]  # pyright: ignore[reportUnknownVariableType]


def _stacked_dep(dep: Any, row: Any, repo: Any, origin: Any) -> Any:
    base = {"task": dep, "branch": row["branch"]}
    elsewhere = row.get("branch_repo") and tick.stack.repo_key(row["branch_repo"]) != origin(repo)
    return {**base, "repo": row["branch_repo"]} if elsewhere else base


def _refund_parked(slug: Any, store: Any, rows: Any, agent: Any) -> Any:
    reason = (store.handoff_envelope(slug, agent.task) or {}).get("reason")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    preempted = tick.preempt.take_victim(store, slug, agent.name)
    if tick._parked(rows.get(agent.task, {}), rows) or reason == "quota" or (reason == "operator" and preempted):
        store.refund_held_claim(slug, agent.task, agent.name, store.claim_generation(slug, agent.task))


def _master_set(doc: Any, task_id: Any) -> Any:
    by = doc.get("_meta", {}).get("stamps", {}).get(f"tasks/{task_id}/state", {}).get("by")
    return "master" if tick.lane_of(by) == tick.MASTER else ""


def _overlaps(mine: Any, theirs: Any) -> Any:
    return bool(tick._shared(mine, theirs))


def _shared(mine: Any, theirs: Any) -> Any:
    pairs = [(a, b) for a in map(tick._area, mine) for b in map(tick._area, theirs)]
    return sorted({b if tick._nested(a, b) else a for a, b in pairs if tick._nested(a, b) or tick._nested(b, a)})


def _sharing(task: Any, rows: Any, agents: Any) -> Any:
    mine = task.get("territory") or []  # pyright: ignore[reportUnknownVariableType]
    owners = {a.task: a.name for a in agents if not tick._ended(a, rows)}
    running = [t for t in rows.values() if t.get("state") in tick.ACTIVE and t["id"] != task["id"]]
    return [
        {
            "task": t["id"],
            "claimant": owners.get(t["id"], ""),
            "areas": areas,
            **({"checks": t["state"] == "pr"} if t["id"] not in owners else {}),
        }
        for t in running
        if (areas := tick._shared(mine, t.get("territory") or []))
    ]


def _area(entry: Any) -> Any:
    return entry.strip().removeprefix("./").rstrip("/")


def _nested(outer: Any, inner: Any) -> Any:
    return inner == outer or inner.startswith(outer + "/")


def _settle(
    slug: str, config: SwarmConfig, store: RedisStore, ledger: Any, rows: dict[str, dict[str, Any]], doc: Any
) -> list[str]:
    agents = store.agents(slug)  # pyright: ignore[reportUnknownMemberType]
    if config.state == "stopping":
        if not any(a.lane == tick.MASTER and a.state != "finished" for a in agents):
            tick._end_awaiting_seats(slug, store, ledger, rows, agents)
            agents = [a for a in agents if a.state != tick.resume.AWAITING]
        if agents:
            return []
        store.update(slug, state="stopped")  # pyright: ignore[reportUnknownMemberType]
        return ["stopped"]
    if any(a.lane != tick.MASTER for a in agents):
        return []
    if config.state != "running" or any(tick._claimable(slug, store, rows, doc, lane) for lane in tick.LANES):
        return []
    store.update(slug, state="drained")  # pyright: ignore[reportUnknownMemberType]
    blocked = sum(1 for t in rows.values() if t.get("state") == "blocked" and not t.get("out_of_scope"))
    waiting = {0: "", 1: ", one blocked task waits for you"}.get(blocked, f", {blocked} blocked tasks wait for you")
    ledger.notify(slug, tick.freeze.notice(doc, rows.values(), tick.freeze.fix_phase(config)) + waiting)
    return ["drained"]


def _end_awaiting_seats(
    slug: str, store: RedisStore, ledger: Any, rows: dict[str, dict[str, Any]], agents: list[AgentRecord]
) -> None:
    for agent in agents:
        if agent.state == tick.resume.AWAITING:
            tick._drop(slug, store, ledger, rows, agent)


def _close_space(slug: Any, config: Any, store: Any, runtime: Any) -> Any:
    if not store.agents(slug):
        runtime.close_space(config)
    return []


def _bin_closed(slug: Any, store: Any, ledger: Any, doc: Any) -> Any:
    if doc.get("closed_at") and not store.agents(slug):
        tick.exits.close_swarm(tick.InboxStore(store.redis), slug)
        if ledger.bin_closed(slug, doc["closed_at"]):
            return ["the closed ledger moved to the bin"]
    return []


tick: Any = import_module("scripts.swarm.tick")
