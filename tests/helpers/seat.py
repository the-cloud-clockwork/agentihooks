"""Seats a test agent on a ledger task through the events path: hub admission admits the task, then the hive's
launched acknowledgement writes its launch decision. No swarm start, no tick pass and no runtime spawn run."""

from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.hub import admission, lease
from scripts.swarm import readiness
from scripts.swarm.store import AgentRecord, RedisStore
from scripts.swarm_ledger.repository import hierarchy

SLUG = "sw"
LAUNCH = {"harness": "claude", "account": "acct", "model": "opus", "effort": "high"}
PROFILES = {"eng": "engineer", "ci": "cicd"}


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
    ``LAUNCH`` with the lane's ``PROFILES`` entry, each key of ``launch`` overriding its default. ``clock`` is the
    admission clock, which names the agent and stamps its ``started_at``.

    The record is admitted and acknowledged, never run by a hive: its state stays ``starting``, ``pane_id`` stays
    empty, ``launched_at`` stays 0, the claim life stays pending, no runtime lists the agent and no launch row is
    written. The swarm stays as created, paused included. ``ledger`` needs ``task``, ``state`` and ``update_task``
    with the ``if_state`` and ``if_match`` guards; a refused admission or acknowledgement fails the test."""
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
    return next(agent for agent in store.agents(slug) if agent.name == admitted["name"])


@pytest.fixture
def seat(env: tuple[RedisStore, Any, Any]) -> Callable[..., AgentRecord]:
    """``seat_agent`` bound to the ``env`` store and ledger of swarm ``sw``: ``seat("t1")`` returns the agent seated
    on t1. A module using it imports ``env`` from ``tests.swarm.test_cli`` beside it."""
    store, ledger, _ = env

    def bound(task_id: str, **options: Any) -> AgentRecord:
        """Seats an agent on ``task_id`` of swarm ``sw``."""
        return seat_agent(store, ledger, SLUG, task_id, **options)

    return bound


def launch_seated(
    store: RedisStore, runtime: Any, agent: AgentRecord, pane: str, at: int, **fields: Any
) -> AgentRecord:
    """Stands in for the hive running ``agent`` of swarm ``sw`` after ``seat_agent``: the record goes ``working`` on
    ``pane``, launched at ``at`` with ``fields`` applied, and the fake ``runtime`` lists it live."""
    launched = replace(agent, pane_id=pane, state="working", launched_at=at, **fields)
    store.execution_registry.update(SLUG, launched)
    runtime.live.add(launched.name)
    return launched
