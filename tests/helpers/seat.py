"""Seats a test agent on a ledger task through the events path: hub admission admits the task, the hive's launched
acknowledgement writes its launch decision and the hub reactor on execution started marks it working. No swarm start,
no tick pass and no runtime spawn run."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.events.envelope import Envelope
from scripts.hub import admission, claims, lease
from scripts.swarm import commands, readiness
from scripts.swarm.store import AgentRecord, RedisStore
from scripts.swarm_ledger.repository import hierarchy

SLUG = "sw"
LAUNCH = {"harness": "claude", "account": "acct", "model": "opus", "effort": "high"}
PROFILES = {"eng": "engineer", "ci": "cicd"}
PANE = "w1:p1"
LAUNCHED_AT = 1_000


def view(store: RedisStore, ledger: Any, slug: str) -> readiness.Snapshot:
    """The snapshot hub admission rechecks, read from the test ledger in place of ``readiness.Source``: every task is
    admitted, so the running swarm state and the release, apply and fleet red waits are not consulted."""
    doc = ledger.state(slug)
    rows = {task["id"]: task for task in doc["tasks"]}
    return readiness.Snapshot(
        slug,
        rows,
        doc,
        hierarchy.project(doc)[0],
        "",
        lambda task_id: store.claimant(slug, task_id) is not None,
        lambda task: True,
    )


@dataclass(frozen=True)
class Launched:
    """How a seated agent is launched: admitted, started and launched at ``at``, and listed by ``runtime``'s ``live``
    set beside its ``started`` launch row when ``runtime`` is given."""

    at: int = LAUNCHED_AT
    runtime: Any = None


def seat_agent(
    store: RedisStore,
    ledger: Any,
    slug: str,
    task_id: str,
    hive: str = readiness.ROOT,
    launch: dict[str, str] | None = None,
    clock: lease.Clock = lease.wall_ms,
) -> AgentRecord:
    """Seats one agent on an open task of a created swarm and returns its record as the events path leaves it.

    The hub ``Admission`` admits the offer for the task's lane and claim generation from ``hive``: it claims the task,
    names the agent, starts its execution in a lane seat, occupies the seat, pends the claim life and marks the ledger
    row claimed by that name. The hive's ``launched`` acknowledgement then writes the launch decision onto the record:
    ``LAUNCH`` with the lane's ``PROFILES`` entry, each key of ``launch`` overriding its default. Last, the hub claim
    lives reactor takes the hive's ``execution.started`` event: the record is ``working`` on pane ``PANE`` launched at
    ``LAUNCHED_AT``, and the claim life is counted. ``clock`` is the admission clock, which names the agent and stamps
    its ``started_at``.

    No runtime lists the agent and no launch row is written. The swarm stays as created, paused included.
    ``ledger`` needs ``task``, ``state`` and ``update_task`` with the ``if_state`` and ``if_match`` guards; a refused
    admission or acknowledgement fails the test."""
    row = ledger.task(slug, task_id)
    request = {"hive": hive, "task": task_id, "lane": row["lane"], "generation": readiness.generation(row)}
    decision = {**LAUNCH, "profile": PROFILES.get(row["lane"], "engineer"), **(launch or {})}
    source = SimpleNamespace(snapshot=lambda name: view(store, ledger, name))
    admitter = admission.Admission(store, ledger, source, clock)  # pyright: ignore[reportArgumentType]
    admitted = admitter.admit(slug, request)
    if not admitted or "name" not in admitted:
        pytest.fail(f"hub admission refused {task_id}: {admitted}")
    if not admitter.launched(slug, {**request, "launch": decision}):
        pytest.fail(f"the launched acknowledgement for {task_id} was refused")
    execution = admitted["task"]
    claims.Claims(store).handle(
        Envelope(
            id=f"started:{execution['execution_id']}",
            type="execution.started",
            swarm=slug,
            entity="execution",
            key=execution["execution_id"],
            version=execution["execution_generation"],
            time=LAUNCHED_AT,
            author=hive,
            data={
                "task": task_id,
                "name": admitted["name"],
                "claim_key": f"seat:{execution['execution_id']}",
                "hive": hive,
                "pane_id": PANE,
                "launched_at": LAUNCHED_AT,
            },
        )
    )
    return next(agent for agent in store.agents(slug) if agent.name == admitted["name"])


def seat_launched(store: RedisStore, ledger: Any, slug: str, task_id: str, hive: str, started: Launched) -> AgentRecord:
    """Seats one agent through ``seat_agent`` from ``hive`` with the admission clock at ``started.at`` and returns it
    launched at that time: given ``started.runtime``, that runtime lists the agent live and the swarm holds its
    ``started`` launch row, so the tick judges it as an agent it already launched."""
    seated = seat_agent(store, ledger, slug, task_id, hive, clock=lambda: started.at)
    agent = replace(seated, launched_at=started.at)
    store.execution_registry.update(slug, agent)
    if started.runtime is not None:
        started.runtime.live.add(agent.name)
        store.record_launch(slug, agent, "started")
    return agent


@pytest.fixture
def seat(env: tuple[RedisStore, Any, Any]) -> Callable[..., AgentRecord]:
    """``seat_agent`` bound to the ``env`` store and ledger of swarm ``sw``: ``seat("t1")`` returns the agent seated
    on t1. A module using it imports ``env`` from ``tests.swarm.test_cli`` beside it."""
    store, ledger, _ = env

    def bound(task_id: str, **options: Any) -> AgentRecord:
        """Seats an agent on ``task_id`` of swarm ``sw``."""
        return seat_agent(store, ledger, SLUG, task_id, **options)

    return bound


def seat_owner(store: RedisStore, ledger: Any, runtime: Any, task_id: str, at: int, **fields: Any) -> AgentRecord:
    """Seats a working owner on ``task_id`` of swarm ``sw`` as its hive leaves it once the pane runs.

    ``seat_agent`` admits it from the hive the tick runs as, at the test clock ``at``, and counts its claim life. The
    record then sits on pane ``w1:<task_id>``, launched at ``at`` with ``fields`` applied, and the fake ``runtime``
    lists it live, so the tick judges a local, execution backed owner of its own hive."""
    seated = seat_agent(store, ledger, SLUG, task_id, hive=commands.hive_id(), clock=lambda: at)
    owner = replace(seated, pane_id=f"w1:{task_id}", state="working", launched_at=at, **fields)
    store.execution_registry.update(SLUG, owner)
    runtime.live.add(owner.name)
    return owner
