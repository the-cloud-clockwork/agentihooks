"""Seats a test agent on a ledger task through the events path: hub admission admits the task, then the hive's
launched acknowledgement writes its launch decision. No swarm start, no tick pass and no runtime spawn run."""

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.hub import admission
from scripts.swarm import readiness
from scripts.swarm.store import AgentRecord, RedisStore
from scripts.swarm_ledger.repository import hierarchy

SLUG = "sw"
LAUNCH = {"harness": "claude", "profile": "engineer", "account": "work", "model": "opus", "effort": "medium"}


def view(store: RedisStore, ledger: Any, slug: str) -> readiness.Snapshot:
    """The readiness snapshot hub admission rechecks, read from the test ledger with every task admitted."""
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
) -> AgentRecord:
    """Seats one agent on an open task of a created swarm and returns its record.

    The hub ``Admission`` admits the offer for the task's lane and claim generation from ``hive``: it claims the task,
    names the agent, starts its execution, occupies its seat and marks the ledger row claimed by that name. The hive's
    ``launched`` acknowledgement then writes ``launch`` (``LAUNCH`` by default) onto the record. The swarm stays as
    created, paused included, and nothing is spawned. ``ledger`` needs ``task``, ``state`` and ``update_task`` with
    the ``if_state`` and ``if_match`` guards; a refused admission or acknowledgement fails the test."""
    row = ledger.task(slug, task_id)
    request = {"hive": hive, "task": task_id, "lane": row["lane"], "generation": readiness.generation(row)}
    source = SimpleNamespace(snapshot=lambda name: view(store, ledger, name))
    admitter = admission.Admission(store, ledger, source)  # pyright: ignore[reportArgumentType]
    admitted = admitter.admit(slug, request)
    if not admitted or "name" not in admitted:
        pytest.fail(f"hub admission refused {task_id}: {admitted}")
    if not admitter.launched(slug, {**request, "launch": launch or LAUNCH}):
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
