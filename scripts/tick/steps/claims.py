# pyright: reportUnusedFunction=false, reportUnusedClass=false
"""Claim order, agent status and the tick's launch records.

Moved code reaches swarm tick module names through ``tick`` so patched tick seams apply.
"""

import json
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


def _prepare(run: Any, lane: Any, task: Any, taken: Any, failing: Any, peers: Any) -> Any:
    slug, config, store, ledger, runtime = run.slug, run.config, run.store, run.ledger, run.runtime
    rows, doc, now_ms = run.rows, run.doc, run.now_ms
    task = rows[task["id"]] = dict(task)
    expected = {"claimed_by": task.get("claimed_by", ""), "claim_generation": task.get("claim_generation", 0)}
    blocked = (
        tick.timing.timed(tick._lives_spent, slug, store, ledger, rows, task)
        or tick.timing.timed(tick._room_wait, runtime, lane, task)
        or tick.timing.timed(tick._held_back, slug, ledger, rows, runtime, task, now_ms)
        or tick.timing.timed(tick._owner_hold, slug, store, runtime, task)
    )
    if blocked:
        return None, [blocked]
    name = store.next_name(slug, lane, now_ms)
    if not tick.timing.timed(store.claim, slug, task["id"], name, tick.LEASE_MS):
        tick._release_admission(slug, runtime, task)
        return None, []
    handoff = store.handoff(slug, task["id"])
    lives = store.earlier_lives(slug, task["id"])
    if handoff:
        task["handoff"] = handoff
        task["handoff_envelope"] = tick.timing.timed(
            tick.continuation.resolve,
            config.repo,
            task,
            rows,
            lives,
            store.handoff_envelope(slug, task["id"]) or {},
            tick._pass_reclaim(run),
        )
    elif lives or task.get("branch"):
        task["reclaim"] = tick.timing.timed(
            tick.continuation.resolve, config.repo, task, rows, lives, {}, tick._pass_reclaim(run)
        )
        store.put_reclaim(slug, name, task["reclaim"])
    task["stack_base"] = tick._stack_base(task, rows, config.repo)
    task["overlaps"] = tick.timing.timed(
        tick._sharing, task, rows, tick.timing.timed(tick._reported, slug, store, runtime, peers)
    )
    task["group"] = [
        {key: rows[m][key] for key in ("id", "title", "description")}
        for m in task.get("group_members") or []  # pyright: ignore[reportUnknownVariableType]
        if m in rows
    ]
    saved = store.redis.hget(store.key(slug, "launch-assignments"), task["id"])
    preferred = json.loads(saved)["seat"] if saved else store.handoff_seat(slug, task["id"])
    seat = tick._free_seat(slug, lane, taken, preferred)
    taken.add(seat)
    record = AgentRecord(name, lane, task["id"], started_at=now_ms, state="starting", seat=seat)
    store.put_agent(slug, record)
    try:
        state = "pr" if task.get("pr_url") else "claimed"
        fields = {
            "state": state,
            "claimed_by": name,
            "workspace": str(tick.timing.timed(tick.ledger_workspace.scaffold, slug, task, doc)),
        }
        kind = config.lanes.get(lane, {}).get("kind", "")
        if kind not in ("", "auto") and not task.get("kind"):
            fields["kind"] = kind
        live = tick.timing.timed(ledger.update_task, slug, task["id"], fields, if_state=("open",), if_match=expected)
        task.update(live)
        if live["claimed_by"] != name:
            tick._release_admission(slug, runtime, task)
            store.release(slug, task["id"], name)
            store.drop_agent(slug, name)
            return None, [f"task {task['id']} is {live['state']} on the ledger, not claimed"]
        task.update(fields)
        tick.timing.timed(store.record_launch, slug, record, "pending")
        store.seats.occupy(seat, name, now_ms)
        task["transfer"] = tick.timing.timed(tick.transfers.attach, store, slug, record)
        primed_task = {
            **tick.timing.timed(tick.primed, store, slug, seat, task),
            tick.ACK_LATER: True,
            "detached_launch": True,
            **tick._steer(slug, store, task, failing),
        }
    except Exception as exc:
        failed = tick.Failed(record, task, exc)
        return None, tick.timing.timed(tick._not_launched, slug, store, ledger, runtime, rows, failed, now_ms)
    return tick.Launch(record, task, primed_task), []


tick: Any = import_module("scripts.swarm.tick")
