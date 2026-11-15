"""Seats a test agent on a ledger task through the events path: hub admission admits the task, the hive's launched
acknowledgement writes its launch decision and the hub reactor on execution started marks it working. No swarm start,
no tick pass and no runtime spawn run."""

from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.events.envelope import Envelope
from scripts.hub import admission, claims
from scripts.swarm import readiness
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
    started: Launched | None = None,
) -> AgentRecord:
    """Seats one agent on an open task of a created swarm and returns its record as the events path leaves it.

    The hub ``Admission`` admits the offer for the task's lane and claim generation from ``hive``: it claims the task,
    names the agent, starts its execution in a lane seat, occupies the seat, pends the claim life and marks the ledger
    row claimed by that name. The hive's ``launched`` acknowledgement then writes the launch decision onto the record:
    ``LAUNCH`` with the lane's ``PROFILES`` entry, each key of ``launch`` overriding its default. Last, the hub claim
    lives reactor takes the hive's ``execution.started`` event: the record is ``working`` on pane ``PANE`` launched at
    ``LAUNCHED_AT``, and the claim life is counted.

    With ``started`` the admission clock reads ``started.at``, the agent launches at it and, given
    ``started.runtime``, that runtime lists the agent and the swarm holds its ``started`` launch row. Without it the
    admission reads the wall clock, no runtime lists the agent and no launch row is written. The swarm stays as
    created, paused included. ``ledger`` needs ``task``, ``state`` and ``update_task`` with the ``if_state`` and
    ``if_match`` guards; a refused admission or acknowledgement fails the test."""
    row = ledger.task(slug, task_id)
    request = {"hive": hive, "task": task_id, "lane": row["lane"], "generation": readiness.generation(row)}
    decision = {**LAUNCH, "profile": PROFILES.get(row["lane"], "engineer"), **(launch or {})}
    source = SimpleNamespace(snapshot=lambda name: view(store, ledger, name))
    clock = {} if started is None else {"clock": lambda: started.at}
    launched_at = LAUNCHED_AT if started is None else started.at
    admitter = admission.Admission(store, ledger, source, **clock)  # pyright: ignore[reportArgumentType]
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
            time=launched_at,
            author=hive,
            data={
                "task": task_id,
                "name": admitted["name"],
                "claim_key": f"seat:{execution['execution_id']}",
                "hive": hive,
                "pane_id": PANE,
                "launched_at": launched_at,
            },
        )
    )
    agent = next(agent for agent in store.agents(slug) if agent.name == admitted["name"])
    if started is not None and started.runtime is not None:
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
