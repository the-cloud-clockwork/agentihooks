"""The status publish tick step publishes the pass's health findings, else the last published ones, and leaves them
for the status report to build only before the swarm's first publish."""

import json
from importlib import import_module
from typing import Any

import pytest

from scripts.swarm import cli
from scripts.swarm.store import RedisStore
from scripts.tick import engine, registry
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context
from scripts.tick.steps import masters, report

pytestmark = pytest.mark.unit

SLUG = "sw"
LAST = [{"kind": "stale claim", "subject": "tasks/t1"}]
FOUND = [{"kind": "idle with claim", "subject": "tasks/t2"}]


@pytest.fixture
def store() -> RedisStore:
    import fakeredis

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


def published(store: RedisStore, findings: list[dict[str, str]]) -> None:
    """Store a published status of swarm ``SLUG`` carrying ``findings``, as the last status publish left it."""
    store.redis.set(store.key(SLUG, "published"), json.dumps({"status": {"findings": findings}, "workspaces": {}}))


def publish(store: RedisStore, box: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Run the closing group's status publish step over the pass ``box`` and return the findings it published."""
    calls: list[Any] = []
    monkeypatch.setattr(cli, "_publish_tick_status", lambda *args: calls.append(args[5]))
    step = next(step for step in report.STEPS if step.name == "status publish")
    state: dict[str, object] = {"store": store, "slug": SLUG, "doc": {}, "view": None, "box": box, "failure": None}
    records = engine.run_group("closing", Context(masters._PassView(state), Deadline.after(5.0)), (step,))
    assert [(record.name, record.outcome) for record in records] == [("status publish", engine.OK)]
    return calls


@pytest.mark.feature("tick-status-publish-last-findings")
def test_status_publish() -> None:
    """The status publish step registers in group closing on the closing lane with its feature and ports."""
    import_module("scripts.tick.steps.report")
    found = {step.name: step for step in registry.steps()}["status publish"]
    assert (found.group, found.lane, found.feature, found.effects) == (
        "closing",
        "closing",
        "tick-status-publish-last-findings",
        frozenset(),
    )


@pytest.mark.feature("tick-status-publish-last-findings")
def test_status_publish_publishes_the_last_findings_when_the_pass_stored_none(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pass whose health findings step stored no findings publishes the last published ones."""
    published(store, LAST)
    assert publish(store, {}, monkeypatch) == [LAST]


@pytest.mark.feature("tick-status-publish-last-findings")
def test_status_publish_leaves_the_findings_to_the_report_before_the_first_publish(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before the swarm's first publish, a pass without stored findings leaves them for the status report to build."""
    assert publish(store, {}, monkeypatch) == [None]


@pytest.mark.feature("tick-status-publish-last-findings")
def test_status_publish_keeps_the_findings_the_pass_stored(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pass whose health findings step stored findings publishes those, never the last published ones."""
    published(store, LAST)
    assert publish(store, {"found": FOUND}, monkeypatch) == [FOUND]
