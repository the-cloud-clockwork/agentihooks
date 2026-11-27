"""The review pass queues one review task per pull request, merge queue group and release a swarm sees."""

import hashlib
from importlib import import_module
from types import SimpleNamespace
from typing import Any, cast

import pytest

from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.events.memory import MemoryBus, MemoryConsumer, open_bus
from scripts.events.subjects import subject
from scripts.review import record
from scripts.swarm.ledger_client import LedgerRefused
from scripts.swarm.store import RedisStore
from tests.review.fakes import store

pytestmark = pytest.mark.unit

SLUG = "crew"
OTHER = "fleet"
REPO = "https://github.com/acme/widgets"
PULL = f"{REPO}/pull/7"
NOW = 1_700_000_000_000
UNLINKED = {"id": "t2", "pr_url": "", "phase": "p2"}


class Ledger:
    def __init__(self, refuse: bool = False) -> None:
        self.added: list[tuple[str, dict[str, Any], str]] = []
        self.refuse = refuse

    def add_task(self, slug: str, task: dict[str, Any], by: str) -> None:
        if self.refuse:
            raise LedgerRefused("refused")
        self.added.append((slug, task, by))


class Store:
    def __init__(self, config: SimpleNamespace) -> None:
        self._config = config

    def config(self, slug: str) -> SimpleNamespace:
        assert slug == SLUG
        return self._config


def review() -> Any:
    return import_module("scripts.tick.steps.review")


def doc(*extra: dict[str, Any]) -> dict[str, Any]:
    return {"tasks": [{"id": "t1", "pr_url": PULL, "phase": "p1"}, *extra], "phases": []}


def publish(
    bus: MemoryBus, kind: str, data: dict[str, Any], key: str = "k", time: int = NOW, swarm: str = SLUG
) -> None:
    event = Envelope(f"{kind}-{key}-{time}", kind, swarm, kind.split(".")[0], key, 1, time, "github", data)
    bus.events.publish(subject(swarm, *kind.split(".")), event)


def run(
    bus: MemoryBus, ledger: Ledger, state: dict[str, Any] | None = None, found: RedisStore | None = None
) -> list[str]:
    return review().review_pass(bus, SLUG, state or doc(), ledger, NOW, store() if found is None else found)


@pytest.mark.feature("swarm-review-trigger")
def test_one_pull_opened_event_queues_exactly_one_review_task_and_a_repeat_queues_none() -> None:
    bus, ledger = open_bus(BusConfig()), Ledger()
    publish(bus, "pull.opened", {"url": PULL}, PULL)
    publish(bus, "pull.opened", {"url": PULL}, PULL, NOW + 1)
    assert len(run(bus, ledger)) == 1
    [(slug, task, by)] = ledger.added
    assert (slug, by, task["lane"], task["kind"], task["phase"]) == (SLUG, "swarm", "review", "review", "p1")
    assert PULL in task["description"] and "t1" in task["description"]
    assert task["title"] == "Review pull request 7 of widgets"
    publish(bus, "pull.opened", {"url": PULL}, PULL, NOW + 2)
    assert run(bus, ledger, doc({"id": task["task"]})) == []
    assert len(ledger.added) == 1


@pytest.mark.feature("swarm-review-trigger")
def test_a_merge_group_and_a_release_each_queue_one_review_task() -> None:
    bus, ledger = open_bus(BusConfig()), Ledger()
    publish(bus, "merge_group.requested", {"repository": REPO, "head": "a1", "base": "b1", "number": 7})
    publish(bus, "release.published", {"repository": REPO, "tag": "v1.2.3", "url": f"{REPO}/releases/tag/v1.2.3"})
    assert len(run(bus, ledger)) == 2
    group, release = (task for _, task, _ in ledger.added)
    assert (group["phase"], f"{REPO}/commit/a1" in group["description"]) == ("p1", True)
    assert (release["phase"], f"{REPO}/releases/tag/v1.2.3" in release["description"]) == ("", True)
    assert len({group["task"], release["task"]}) == 2
    assert group["title"] == "Review the merge queue group for pull request 7 of widgets"
    assert "No swarm task carries this change." in release["description"]


@pytest.mark.feature("swarm-review-trigger")
def test_a_scoped_bus_review_pass_queues_only_its_own_swarm_events() -> None:
    bus = open_bus(BusConfig(scoped=True))
    events: dict[str, list[tuple[str, dict[str, Any]]]] = {
        swarm: [
            ("pull.opened", {"url": f"{REPO}/pull/{number}"}),
            ("merge_group.requested", {"repository": REPO, "head": f"h{number}", "base": "b1", "number": number}),
            ("release.published", {"repository": REPO, "tag": f"v{number}", "url": f"{REPO}/releases/tag/v{number}"}),
        ]
        for swarm, number in ((SLUG, 7), (OTHER, 8))
    }
    for swarm, changes in events.items():
        for kind, data in changes:
            publish(bus, kind, data, key=swarm, swarm=swarm)
    for swarm, changes in events.items():
        ledger = Ledger()
        assert len(review().review_pass(bus, swarm, doc(), ledger, NOW, store())) == 3
        expected = {review().review_task(review().KINDS[kind], data, doc())["task"] for kind, data in changes}
        assert {task["task"] for _, task, _ in ledger.added} == expected
        assert {slug for slug, _, _ in ledger.added} == {swarm}


@pytest.mark.feature("swarm-review-trigger")
def test_a_pull_review_task_names_its_change_and_author_exactly() -> None:
    task = review().review_task("pr", {"url": PULL}, doc())
    assert task["task"] == "review-pr-" + hashlib.sha256(PULL.lower().encode()).hexdigest()[:12]
    assert task["description"] == f"Review the pull request {PULL}. Author task t1."


@pytest.mark.feature("swarm-review-trigger")
def test_a_release_review_task_names_its_tag_and_no_author() -> None:
    url = f"{REPO}/releases/tag/v1.2.3"
    task = review().review_task("rel", {"repository": REPO, "tag": "v1.2.3", "url": url}, doc(UNLINKED))
    assert (task["title"], task["phase"]) == ("Review release v1.2.3 of widgets", "")
    assert task["description"] == f"Review the release {url}. No swarm task carries this change."


@pytest.mark.feature("swarm-review-trigger")
def test_a_merge_group_naming_no_pull_request_has_no_author() -> None:
    task = review().review_task("mq", {"repository": REPO, "head": "a1", "number": None}, doc(UNLINKED))
    assert (task["title"], task["phase"]) == ("Review the merge queue group of widgets", "")


@pytest.mark.feature("swarm-review-trigger")
def test_an_author_task_without_a_phase_gives_the_review_no_phase() -> None:
    task = review().review_task("pr", {"url": PULL}, {"tasks": [{"id": "t1", "pr_url": PULL}]})
    assert task["phase"] == ""


@pytest.mark.feature("swarm-review-trigger")
def test_the_durable_consumer_is_named_by_kind_and_swarm() -> None:
    assert review()._durable(SLUG, "pr") == "review-pr-" + hashlib.sha256(SLUG.encode()).hexdigest()[:16]


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
def test_an_event_at_the_edge_of_the_replay_window_still_queues() -> None:
    bus, ledger = open_bus(BusConfig()), Ledger()
    publish(bus, "pull.opened", {"url": PULL}, PULL, NOW - review().REPLAY_MS)
    assert len(run(bus, ledger)) == 1


@pytest.mark.feature("swarm-review-trigger")
def test_a_backlog_older_than_the_replay_window_queues_when_the_consumer_is_older_still() -> None:
    bus, ledger = open_bus(BusConfig(), wall=lambda: NOW - review().REPLAY_MS - 60_000), Ledger()
    publish(bus, "pull.opened", {"url": PULL}, PULL, NOW - review().REPLAY_MS - 1)
    assert len(run(bus, ledger)) == 1
    assert ledger.added[0][1]["task"] == review().review_task("pr", {"url": PULL}, doc())["task"]


@pytest.mark.feature("swarm-review-trigger")
def test_an_event_older_than_the_window_and_the_consumer_queues_nothing() -> None:
    created = NOW - review().REPLAY_MS - 60_000
    bus, ledger = open_bus(BusConfig(), wall=lambda: created), Ledger()
    publish(bus, "pull.opened", {"url": PULL}, PULL, created - 1)
    assert run(bus, ledger) == []
    assert ledger.added == []


@pytest.mark.feature("swarm-review-trigger")
def test_a_consumer_with_no_created_time_keeps_the_replay_window_rule() -> None:
    bus, ledger = open_bus(BusConfig(), wall=lambda: NOW - review().REPLAY_MS - 60_000), Ledger()
    consumer = bus.events.consumer(review()._durable(SLUG, "pr"), subject(SLUG, "pull", "opened"))
    cast(MemoryConsumer, consumer).created = None
    publish(bus, "pull.opened", {"url": PULL}, PULL, NOW - review().REPLAY_MS - 1)
    assert run(bus, ledger) == []
    assert ledger.added == []


@pytest.mark.feature("swarm-review-trigger")
def test_a_backlog_older_than_the_cap_queues_nothing_even_when_the_consumer_is_older_still() -> None:
    bus, ledger = open_bus(BusConfig(), wall=lambda: NOW - review().BACKLOG_MS - 60_000), Ledger()
    publish(bus, "pull.opened", {"url": PULL}, PULL, NOW - review().BACKLOG_MS - 1)
    assert run(bus, ledger) == []
    assert ledger.added == []


@pytest.mark.feature("swarm-review-trigger")
def test_a_backlog_event_at_the_cap_still_queues() -> None:
    bus, ledger = open_bus(BusConfig(), wall=lambda: NOW - review().BACKLOG_MS - 60_000), Ledger()
    publish(bus, "pull.opened", {"url": PULL}, PULL, NOW - review().BACKLOG_MS)
    assert len(run(bus, ledger)) == 1


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
    module, bus, ledger = review(), open_bus(BusConfig()), Ledger()
    publish(bus, "pull.opened", {"url": PULL}, PULL)
    monkeypatch.setattr(module, "_bus", lambda: bus)
    values = {"store": Store(SimpleNamespace(state=state, max_review=cap)), "slug": SLUG, "ledger": ledger}
    assert module._review_pass(SimpleNamespace(), values.__getitem__) == []
    assert ledger.added == []


@pytest.mark.feature("swarm-review-trigger")
def test_the_step_runs_the_pass_on_the_swarm_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    module, bus, ledger = review(), open_bus(BusConfig()), Ledger()
    publish(bus, "pull.opened", {"url": PULL}, PULL)
    monkeypatch.setattr(module, "_bus", lambda: bus)
    values = {
        "store": Store(SimpleNamespace(state="running", max_review=1)),
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


@pytest.mark.feature("swarm-review-trigger")
def test_the_step_opens_the_configured_shared_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    module, opened = review(), []
    module._bus.cache_clear()
    monkeypatch.setenv("AGENTIHOOKS_BUS", "nats")
    monkeypatch.setattr(module, "open_bus", lambda config: opened.append(config) or "bus")
    assert module._bus() == "bus"
    assert [config.backend for config in opened] == ["nats"]
    module._bus.cache_clear()


@pytest.mark.feature("review-queue-group-post")
def test_the_review_pass_records_each_merge_group_it_queues_a_review_task_for() -> None:
    bus, ledger, found = open_bus(BusConfig()), Ledger(), store()
    publish(bus, "merge_group.requested", {"repository": REPO, "head": "a1", "base": "b1", "number": 7}, "a1")
    publish(bus, "merge_group.requested", {"repository": REPO, "head": "a2", "base": "b2", "number": None}, "a2")
    publish(bus, "pull.opened", {"url": PULL}, PULL)
    publish(bus, "release.published", {"repository": REPO, "tag": "v1", "url": f"{REPO}/releases/tag/v1"}, "v1")
    assert len(run(bus, ledger, found=found)) == 4
    assert record.group_of(found, SLUG, "a1") == record.Group(REPO, "a1", "b1", 7)
    assert record.group_of(found, SLUG, "a2") == record.Group(REPO, "a2", "b2", 0)
    assert sorted(found.redis.keys(found.key(SLUG, "review", "group", "*"))) == [
        found.key(SLUG, "review", "group", "a1"),
        found.key(SLUG, "review", "group", "a2"),
    ]


@pytest.mark.feature("review-queue-group-post")
def test_a_merge_group_whose_review_task_the_ledger_holds_still_records_its_group() -> None:
    bus, ledger, found = open_bus(BusConfig()), Ledger(), store()
    data = {"repository": REPO, "head": "a1", "base": "b1", "number": 7}
    publish(bus, "merge_group.requested", data, "a1")
    queued = review().review_task("mq", data, doc())["task"]
    assert run(bus, ledger, doc({"id": queued}), found) == []
    assert ledger.added == []
    assert record.group_of(found, SLUG, "a1") == record.Group(REPO, "a1", "b1", 7)


@pytest.mark.feature("review-queue-group-post")
def test_the_step_records_each_merge_group_in_the_swarm_store(monkeypatch: pytest.MonkeyPatch) -> None:
    module, bus, ledger, found = review(), open_bus(BusConfig()), Ledger(), store()
    publish(bus, "merge_group.requested", {"repository": REPO, "head": "a1", "base": "b1", "number": 7}, "a1")
    monkeypatch.setattr(module, "_bus", lambda: bus)
    monkeypatch.setattr(found, "config", lambda slug: SimpleNamespace(state="running", max_review=1))
    values = {"store": found, "slug": SLUG, "doc": doc(), "ledger": ledger}
    assert len(module._review_pass(SimpleNamespace(now_ms=lambda: NOW), values.__getitem__)) == 1
    assert record.group_of(found, SLUG, "a1") == record.Group(REPO, "a1", "b1", 7)
