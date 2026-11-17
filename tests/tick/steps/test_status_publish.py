"""The status publish tick step publishes the pass's health findings, else the last published ones, and leaves them
for the status report to build only in a failed pass and before the swarm's first publish."""

from importlib import import_module
from typing import Any

import pytest

from scripts.swarm import cli, commands
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
    """Publish a status of swarm ``SLUG`` carrying ``findings``, as the last status publish left it."""
    commands.publish(store, SLUG, {"findings": findings}, {})


def publish(
    store: RedisStore, box: dict[str, Any], monkeypatch: pytest.MonkeyPatch, failure: BaseException | None = None
) -> list[Any]:
    """Run the closing group's status publish step over the pass ``box`` and ``failure`` and return the findings it
    handed the publisher."""
    calls: list[Any] = []

    def handed(store: Any, slug: str, doc: Any, view: Any, failure: Any, found: Any) -> None:
        calls.append(found)

    monkeypatch.setattr(cli, "_publish_tick_status", handed)
    step = next(step for step in report.STEPS if step.name == "status publish")
    state: dict[str, object] = {"store": store, "slug": SLUG, "doc": {}, "view": None, "box": box, "failure": failure}
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
def test_status_publish_leaves_the_findings_to_the_report_when_the_pass_failed(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed pass leaves its findings for the status report to build even after a publish, so an alarm about the
    failing ticks reaches the page."""
    published(store, LAST)
    assert publish(store, {}, monkeypatch, RuntimeError("forced phase failure")) == [None]


@pytest.mark.feature("tick-status-publish-last-findings")
def test_status_publish_keeps_the_findings_the_pass_stored(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pass whose health findings step stored findings publishes those, never the last published ones."""
    published(store, LAST)
    assert publish(store, {"found": FOUND}, monkeypatch) == [FOUND]
