"""The review pass queues one review task per pull request, merge queue group and release a swarm sees."""

from importlib import import_module
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.events.memory import MemoryBus, open_bus
from scripts.events.subjects import subject
from scripts.swarm.ledger_client import LedgerRefused

pytestmark = pytest.mark.unit

SLUG = "crew"
REPO = "https://github.com/acme/widgets"
PULL = f"{REPO}/pull/7"
NOW = 10_000_000


class Ledger:
    def __init__(self, refuse: bool = False) -> None:
        self.added: list[tuple[str, dict[str, Any], str]] = []
        self.refuse = refuse

    def add_task(self, slug: str, task: dict[str, Any], by: str) -> None:
        if self.refuse:
            raise LedgerRefused("refused")
        self.added.append((slug, task, by))


def review() -> Any:
    return import_module("scripts.tick.steps.review")


def doc(*extra: dict[str, Any]) -> dict[str, Any]:
    return {"tasks": [{"id": "t1", "pr_url": PULL, "phase": "p1"}, *extra], "phases": []}


def publish(bus: MemoryBus, kind: str, data: dict[str, Any], key: str = "k", time: int = NOW) -> None:
    event = Envelope(f"{kind}-{key}-{time}", kind, SLUG, kind.split(".")[0], key, 1, time, "github", data)
    bus.events.publish(subject(SLUG, *kind.split(".")), event)


def run(bus: MemoryBus, ledger: Ledger, state: dict[str, Any] | None = None) -> list[str]:
    return review().review_pass(bus, SLUG, state or doc(), ledger, NOW)


@pytest.mark.feature("swarm-review-trigger")
def test_one_pull_opened_event_queues_exactly_one_review_task_and_a_repeat_queues_none() -> None:
    bus, ledger = open_bus(BusConfig()), Ledger()
    publish(bus, "pull.opened", {"url": PULL}, PULL)
    publish(bus, "pull.opened", {"url": PULL}, PULL, NOW + 1)
    assert len(run(bus, ledger)) == 1
    [(slug, task, by)] = ledger.added
    assert (slug, by, task["lane"], task["kind"], task["phase"]) == (SLUG, "swarm", "review", "review", "p1")
    assert PULL in task["description"] and "t1" in task["description"]
    publish(bus, "pull.opened", {"url": PULL}, PULL, NOW + 2)
    assert run(bus, ledger, doc({"id": task["task"]})) == []
    assert len(ledger.added) == 1


@pytest.mark.feature("swarm-review-trigger")
def test_a_merge_group_and_a_release_each_queue_one_review_task() -> None:
    bus, ledger = open_bus(BusConfig()), Ledger()
    publish(bus, "merge_group.requested", {"repository": REPO, "head": "a1", "number": 7})
    publish(bus, "release.published", {"repository": REPO, "tag": "v1.2.3", "url": f"{REPO}/releases/tag/v1.2.3"})
    assert len(run(bus, ledger)) == 2
    group, release = (task for _, task, _ in ledger.added)
    assert (group["phase"], f"{REPO}/commit/a1" in group["description"]) == ("p1", True)
    assert (release["phase"], f"{REPO}/releases/tag/v1.2.3" in release["description"]) == ("", True)
    assert len({group["task"], release["task"]}) == 2


@pytest.mark.feature("swarm-review-trigger")
def test_review_task_ids_are_keyed_by_the_change_alone() -> None:
    first, second = Ledger(), Ledger()
    for ledger in (first, second):
        bus = open_bus(BusConfig())
        publish(bus, "pull.opened", {"url": PULL}, PULL, NOW + len(ledger.added))
        run(bus, ledger)
    assert first.added[0][1]["task"] == second.added[0][1]["task"]


@pytest.mark.feature("swarm-review-trigger")
def test_events_from_before_the_replay_window_queue_nothing() -> None:
    bus, ledger = open_bus(BusConfig()), Ledger()
    publish(bus, "pull.opened", {"url": PULL}, PULL, NOW - review().REPLAY_MS - 1)
    assert run(bus, ledger) == []
    assert ledger.added == []


@pytest.mark.feature("swarm-review-trigger")
def test_a_ledger_refusal_is_skipped_and_never_fails_the_pass(capsys: pytest.CaptureFixture[str]) -> None:
    bus = open_bus(BusConfig())
    publish(bus, "pull.opened", {"url": PULL}, PULL)
    assert run(bus, Ledger(refuse=True)) == []
    assert "refused" in capsys.readouterr().err


@pytest.mark.feature("swarm-review-trigger")
@pytest.mark.parametrize(("state", "cap"), [("running", 0), ("stopped", 2)])
def test_the_step_is_inert_without_a_review_lane_or_a_running_swarm(
    monkeypatch: pytest.MonkeyPatch, state: str, cap: int
) -> None:
    module = review()
    monkeypatch.setattr(module, "_bus", lambda: pytest.fail("the step opened a bus"))
    values = {"config": SimpleNamespace(state=state, max_review=cap)}
    assert module._review_pass(SimpleNamespace(), values.__getitem__) == []


@pytest.mark.feature("swarm-review-trigger")
def test_the_step_runs_the_pass_on_the_swarm_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    module, bus, ledger = review(), open_bus(BusConfig()), Ledger()
    publish(bus, "pull.opened", {"url": PULL}, PULL)
    monkeypatch.setattr(module, "_bus", lambda: bus)
    values = {
        "config": SimpleNamespace(state="running", max_review=1),
        "slug": SLUG,
        "doc": doc(),
        "ledger": ledger,
    }
    tick = SimpleNamespace(now_ms=lambda: NOW)
    assert len(module._review_pass(tick, values.__getitem__)) == 1


@pytest.mark.feature("swarm-review-trigger")
def test_the_step_opens_no_bus_on_the_memory_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    module = review()
    module._bus.cache_clear()
    monkeypatch.delenv("AGENTIHOOKS_BUS", raising=False)
    assert module._bus() is None
    module._bus.cache_clear()
