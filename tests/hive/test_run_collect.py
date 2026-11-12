"""The sending scheduler collects each hive run it sent once the hive reports it, then forgets the order."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from hooks import config
from scripts.hive import run_collect
from scripts.workflows import scheduled, store

pytestmark = pytest.mark.unit

KEY = "proof:scheduler:run-orders-sent"
HIVE = "hive-a"
RUN = "entry-1:slot-1"
OWNER = "me"


@pytest.fixture
def redis() -> Any:
    import fakeredis

    return fakeredis.FakeRedis(decode_responses=True)


class Collect:
    def __init__(self, *answers: bool | Exception) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, redis: Any, hive: str, run_id: str, owner: str) -> bool:
        self.calls.append((hive, run_id, owner))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class Inbox:
    def __init__(self) -> None:
        self.sent: list[tuple[str, ...]] = []

    def send(self, *args: str, **options: object) -> str:
        self.sent.append(args)
        return "item"

    def get(self, item_id: str) -> str:
        return f"existing {item_id}"


def sent(redis: Any, run_id: str = RUN) -> None:
    run_collect.remember(redis, KEY, HIVE, run_id, OWNER)
    redis.hset(scheduled.receipt(HIVE, run_id), "sent", "1")


@pytest.mark.feature("hive-run-collect")
def test_remember_records_the_hive_and_owner_of_a_sent_run(redis: Any) -> None:
    run_collect.remember(redis, KEY, HIVE, RUN, OWNER)

    assert redis.hgetall(KEY) == {RUN: json.dumps({"hive": HIVE, "owner": OWNER})}


@pytest.mark.feature("hive-run-collect")
def test_a_collected_run_is_returned_and_forgotten(redis: Any) -> None:
    sent(redis)
    collect = Collect(True)

    assert run_collect.poll(redis, KEY, collect) == [RUN]

    assert collect.calls == [(HIVE, RUN, OWNER)]
    assert redis.hgetall(KEY) == {}


@pytest.mark.feature("hive-run-collect")
def test_a_run_the_hive_has_not_reported_stays_for_the_next_pass(redis: Any) -> None:
    sent(redis)

    assert run_collect.poll(redis, KEY, Collect(False)) == []

    assert list(redis.hgetall(KEY)) == [RUN]


@pytest.mark.feature("hive-run-collect")
def test_an_expired_receipt_drops_its_run(redis: Any) -> None:
    run_collect.remember(redis, KEY, HIVE, RUN, OWNER)

    assert run_collect.poll(redis, KEY, Collect(False)) == []

    assert redis.hgetall(KEY) == {}


@pytest.mark.feature("hive-run-collect")
def test_a_run_another_pass_already_noticed_is_dropped(redis: Any) -> None:
    sent(redis)
    redis.hset(scheduled.receipt(HIVE, RUN), scheduled.NOTICED, "1")

    assert run_collect.poll(redis, KEY, Collect(False)) == []

    assert redis.hgetall(KEY) == {}


@pytest.mark.feature("hive-run-collect")
def test_a_refused_run_folder_drops_only_that_run(redis: Any) -> None:
    sent(redis, "bad")
    sent(redis, RUN)
    collect = Collect(ValueError("run file ../escape leaves the run folder"), True)

    assert run_collect.poll(redis, KEY, collect) == [RUN]

    assert collect.calls == [(HIVE, "bad", OWNER), (HIVE, RUN, OWNER)]
    assert redis.hgetall(KEY) == {}


@pytest.mark.feature("hive-run-collect")
def test_poll_defaults_to_the_scheduled_collect(redis: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    sent(redis)
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        scheduled, "collect", lambda r, hive, run_id, owner: calls.append((hive, run_id, owner)) or True
    )

    assert run_collect.poll(redis, KEY) == [RUN]

    assert calls == [(HIVE, RUN, OWNER)]


@pytest.mark.feature("hive-run-collect")
def test_a_reported_hive_run_reaches_its_owner_from_the_sending_host(
    redis: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "AGENTIHOOKS_HOME", str(tmp_path / "hive"))
    folder = tmp_path / "hive" / "workflows" / "runs" / RUN
    folder.mkdir(parents=True)
    store.save(folder, {"status": "passed"})
    sent(redis)
    scheduled.report(HIVE, RUN, redis)
    monkeypatch.setattr(config, "AGENTIHOOKS_HOME", str(tmp_path / "sender"))
    inbox = Inbox()

    def collect(r: Any, hive: str, run_id: str, owner: str) -> bool:
        return scheduled.collect(r, hive, run_id, owner, inbox=cast(Any, inbox))

    assert run_collect.poll(redis, KEY, collect) == [RUN]
    assert run_collect.poll(redis, KEY, collect) == []

    restored = tmp_path / "sender" / "workflows" / "runs" / RUN
    assert json.loads((restored / store.STATE_FILE).read_text()) == {"status": "passed"}
    assert inbox.sent == [("scheduler", OWNER, f"workflow run {RUN} passed on hive {HIVE}, run folder {restored}")]


@pytest.mark.feature("hive-run-collect")
def test_a_hive_slot_remembers_the_run_order_it_sent(redis: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.events.redis_state import RedisStateStore
    from scripts.hive import run_placement
    from scripts.scheduler.core import Scheduler
    from scripts.scheduler.slots import WorkflowSlot

    state = RedisStateStore(redis, "scheduler")
    monkeypatch.setattr(run_placement, "dispatch", lambda run, r: HIVE)
    slot = WorkflowSlot("nightly", "/case.md", "/repo", "hive", ("build",))

    slot.deliver({"owner": OWNER}, RUN, state)

    key = Scheduler(state).key(run_collect.SENT)
    assert redis.hgetall(key) == {RUN: json.dumps({"hive": HIVE, "owner": OWNER})}


@pytest.mark.feature("hive-run-collect")
def test_a_hive_slot_with_no_fitting_hive_remembers_nothing(redis: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.events.redis_state import RedisStateStore
    from scripts.hive import run_placement
    from scripts.hive.placement import Wait
    from scripts.scheduler.core import Scheduler
    from scripts.scheduler.slots import WorkflowSlot

    state = RedisStateStore(redis, "scheduler")
    monkeypatch.setattr(run_placement, "dispatch", lambda run, r: Wait("no live hive lists the repo /repo"))
    slot = WorkflowSlot("nightly", "/case.md", "/repo", "hive", ("build",))

    slot.deliver({"owner": OWNER}, RUN, state)

    assert redis.hgetall(Scheduler(state).key(run_collect.SENT)) == {}


@pytest.mark.feature("hive-run-collect")
def test_collect_runs_polls_the_index_of_its_own_machine(redis: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.events.redis_state import RedisStateStore
    from scripts.scheduler.core import Scheduler

    scheduler = Scheduler(RedisStateStore(redis, "scheduler"), machine="proof")
    other = Scheduler(RedisStateStore(redis, "scheduler"), machine="other")
    run_collect.remember(redis, scheduler.key(run_collect.SENT), HIVE, RUN, OWNER)
    run_collect.remember(redis, other.key(run_collect.SENT), HIVE, "theirs", OWNER)
    calls: list[str] = []
    monkeypatch.setattr(scheduled, "collect", lambda r, hive, run_id, owner: calls.append(run_id) or True)

    assert scheduler.collect_runs() == 1

    assert calls == [RUN]


@pytest.mark.feature("hive-run-collect")
async def test_every_runner_pass_collects_after_dispatching(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from scripts.scheduler import service

    monkeypatch.setattr(service, "runtime_dir", lambda *_args: tmp_path)
    stop, calls = asyncio.Event(), []

    def drain() -> None:
        calls.append("dispatched")
        stop.set()

    scheduler = SimpleNamespace(run_due=drain, collect_runs=lambda: calls.append("collected"))

    await service.run(cast(Any, scheduler), stop)

    assert calls == ["dispatched", "collected"]
