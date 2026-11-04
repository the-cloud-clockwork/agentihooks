"""A step abandoned with write domains beyond its own actions journals one swarm_tick_step_abandoned line."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Event
from typing import Any

import pytest

from scripts.swarm import timing
from scripts.tick import engine, pool
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain
from scripts.tick.steps import CallStep
from scripts.tick.steps.tasks import WriteStep

pytestmark = pytest.mark.unit

ABANDONED = "swarm_tick_step_abandoned"


class Redis:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict[str, Any]]] = []

    def xadd(self, key: str, fields: dict[str, str], **kwargs: Any) -> None:
        self.rows.append((key, json.loads(fields["record"])))


class Store:
    def __init__(self) -> None:
        self.redis = Redis()

    def key(self, slug: str, stream: str) -> str:
        return f"{slug}:{stream}"


class View:
    def read(self, domain: Domain, key: str) -> object | None:
        return None


@contextmanager
def recording(store: Store) -> Iterator[None]:
    current = {"pass_id": "p1", "start_ms": 1000, "end_ms": 61000, "wall_s": 60.0, "failed_steps": []}
    token = timing.PASS.set(current)
    try:
        with timing.tick("sw", store):
            yield
    finally:
        timing.PASS.reset(token)


def waiting(release: Event) -> Any:
    def call(tick: Any, get: Any) -> list[object]:
        release.wait(300)
        return []

    return call


def journal(err: str) -> list[dict[str, Any]]:
    found = []
    for line in err.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("event") == ABANDONED:
            found.append(row)
    return found


def joined(name: str) -> None:
    worker = engine._RUNNING.get(("sw", name))
    if worker is not None:
        worker.join(5)


@pytest.mark.feature("swarm-tick-cost-report")
def test_a_write_step_left_at_its_budget_journals_its_returned_write_domains(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store, release = Store(), Event()
    step = WriteStep("rank pass", "planted", waiting(release), writes=frozenset({Domain("tasks"), Domain("ranks")}))
    step.budget_s = 1.0
    try:
        with recording(store):
            engine.run_group("planted", Context(View(), Deadline.after(60.0)), [step])
    finally:
        release.set()
        joined("rank pass")
    lines = journal(capsys.readouterr().err)
    assert [(row["slug"], row["step"], row["budget_s"], row["writes"]) for row in lines] == [
        ("sw", "rank pass", 1.0, ["ranks", "tasks"])
    ]


@pytest.mark.feature("swarm-tick-cost-report")
def test_a_write_step_left_at_an_expired_lane_journals_its_returned_write_domains(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store, release = Store(), Event()
    step = WriteStep("grouping release", "planted", waiting(release), writes=frozenset({Domain("tasks")}))
    step.budget_s = 30.0
    try:
        with recording(store), pool.running(critical_s=0.3):
            engine.run_group("planted", Context(View(), Deadline.after(60.0)), [step])
    finally:
        release.set()
        joined("grouping release")
    assert [(row["step"], row["budget_s"], row["writes"]) for row in journal(capsys.readouterr().err)] == [
        ("grouping release", 30.0, ["tasks"])
    ]


@pytest.mark.feature("swarm-tick-cost-report")
def test_a_step_left_with_only_its_actions_domain_journals_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    store, release = Store(), Event()
    step = CallStep("release alert", "planted", waiting(release))
    step.budget_s = 0.05
    try:
        with recording(store):
            engine.run_group("planted", Context(View(), Deadline.after(60.0)), [step])
    finally:
        release.set()
        joined("release alert")
    assert journal(capsys.readouterr().err) == []
