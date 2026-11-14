"""Each launch path read runs keyed or paged on a store that refuses a whole hash read."""

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm import (
    dev_red,
    dispatch_seat,
    failed_panes,
    hash_pages,
    launch_check,
    local_slots,
    preempt,
    prerequisite_wait,
    remote_control,
    run_window,
)
from scripts.swarm import tick as swarm_tick
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError

SLUG = "sw"
LAUNCH_CHECKED = True


GUARDED = (
    "h",
    swarm_tick.MASTER_WAITING,
    "launch-checks",
    "launch-check-reports",
    preempt.ASKED,
    "prerequisite-waits",
    failed_panes.HELD,
    run_window.KEY,
    "dev-red",
    dispatch_seat.SENT,
    remote_control.FAILURES,
    "launches",
)


def _guarded(name: Any) -> bool:
    return ":local-slots:" in name or any(name == key or name.endswith(f":{key}") for key in GUARDED)


def _refusing() -> Any:
    from fakeredis import FakeRedis

    class Refusing(FakeRedis):
        """A Redis double whose whole read of a launch path hash fails the test."""

        def hgetall(self, name: Any) -> Any:
            """Refuse a whole read of a launch path hash."""
            assert not _guarded(name), f"whole hash read of {name}"
            return super().hgetall(name)

        def hkeys(self, name: Any) -> Any:
            """Refuse a whole field listing of a launch path hash."""
            assert not _guarded(name), f"whole hash field listing of {name}"
            return super().hkeys(name)

    return Refusing(decode_responses=True)


@pytest.fixture
def store() -> RedisStore:
    """A swarm store on a Redis double that refuses whole reads of launch path hashes."""
    created = RedisStore(_refusing())
    created.create(SwarmConfig(SLUG, "/repo", max_eng=2, max_ci=1))
    return created


def _agent(name: str, task: str, **fields: Any) -> AgentRecord:
    return AgentRecord(name=name, task=task, lane="eng", started_at=1, **fields)


@pytest.mark.feature("swarm-launch-bounded-reads")
def test_fields_reads_every_field_across_pages(store: RedisStore) -> None:
    store.redis.hset("h", mapping={f"f{n}": str(n) for n in range(7)})
    assert hash_pages.fields(store.redis, "h", page=2) == {f"f{n}": str(n) for n in range(7)}
    assert hash_pages.fields(store.redis, "missing") == {}


@pytest.mark.feature("swarm-launch-bounded-reads")
def test_fields_stops_after_its_page_bound() -> None:
    calls: list[int] = []
    counts: set[int] = set()

    def hscan(key: str, cursor: int, count: int) -> tuple[int, dict[str, str]]:
        calls.append(cursor)
        counts.add(count)
        return len(calls), {f"f{len(calls)}": "1"}

    with pytest.raises(SwarmError, match="hash h holds more than 1000 pages of 1 fields"):
        hash_pages.fields(SimpleNamespace(hscan=hscan), "h", page=1)
    assert len(calls) == hash_pages.PAGES == 1000
    assert calls[:3] == [0, 1, 2]
    assert counts == {1}


@pytest.mark.feature("swarm-launch-bounded-reads")
def test_picked_returns_only_the_named_fields_that_are_set(store: RedisStore) -> None:
    store.redis.hset("h", mapping={"a": "1", "b": "2", "c": "3"})
    assert hash_pages.picked(store.redis, "h", ["a", "c", "z", "a"]) == {"a": "1", "c": "3"}
    assert hash_pages.picked(store.redis, "h", []) == {}


@pytest.mark.feature("swarm-launch-bounded-reads")
def test_picked_refuses_a_reply_that_misses_a_named_field() -> None:
    client = SimpleNamespace(hmget=lambda key, names: ["1"])
    with pytest.raises(ValueError, match="zip"):
        hash_pages.picked(client, "h", ["a", "b"])


@pytest.mark.feature("swarm-launch-bounded-reads")
def test_launch_check_reads_pending_checks_and_reports_paged(store: RedisStore) -> None:
    launch_check.begin(store, SLUG, _agent("engineer@1-1", "t1"), 10)
    held = _agent("engineer@1-2", "t2")
    launch_check.record(store, SLUG, held, {"profile": {"expected": "a", "actual": "b"}}, 20, 5, held=True)
    launch_check.record(store, SLUG, _agent("engineer@1-3", "t3"), {}, 30, 5)
    assert launch_check.pending(store, SLUG) == {"engineer@1-1": {"task": "t1", "at": 10, "relaunch": True}}
    assert sorted(found["agent"] for found in launch_check.reports(store, SLUG)) == ["engineer@1-2", "engineer@1-3"]
    assert launch_check.judged(store, SLUG) == {"engineer@1-1", "engineer@1-2"}
    assert [finding.subject.split("/")[0] for finding in launch_check.findings(store, SLUG)] == ["profile"]


@pytest.mark.feature("swarm-launch-bounded-reads")
def test_local_slots_count_and_claim_held_slots_paged(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_slots, "live", lambda: set())
    now = local_slots.lease.now_ms(store)
    store.redis.hset(local_slots.key("m"), mapping={f"{SLUG}/a": str(now), f"{SLUG}/b": str(now - 10**9)})
    assert local_slots.held(store, "m", SLUG, "t1") == 1
    assert local_slots.occupancy(store, "m") == (0, 1)
    assert local_slots.claim(store, "m", SLUG, "t1", "c", 5) == 1
    assert sorted(hash_pages.fields(store.redis, local_slots.key("m"))) == [f"{SLUG}/a", f"{SLUG}/c"]


@pytest.mark.feature("swarm-launch-bounded-reads")
def test_preempt_keeps_only_asks_of_live_holders(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scripts.hive.placement_waits.read", lambda store, slug: {})
    key = store.key(SLUG, preempt.ASKED)
    store.redis.hset(key, mapping={"engineer@1-1": json.dumps(["t9", 1]), "engineer@1-2": json.dumps(["t8", 1])})
    full = {"eng": ([], [_agent("engineer@1-1", "t1")])}
    assert preempt.step(SLUG, store, {}, {"tasks": []}, full, 2) == []
    assert sorted(hash_pages.fields(store.redis, key)) == ["engineer@1-1"]


@pytest.mark.feature("swarm-launch-bounded-reads")
def test_prerequisite_wait_reopen_pass_drops_a_stale_blocked_record(store: RedisStore) -> None:
    record = {"agent": "engineer@1-1", "generation": 1, "target": "x", "blocked": True}
    store.redis.hset(prerequisite_wait.key(SLUG), mapping={"t1": json.dumps(record), "t2": json.dumps({})})
    assert prerequisite_wait.reopen_pass(SLUG, store, None, {"t1": {"state": "open"}}) == []
    assert sorted(hash_pages.fields(store.redis, prerequisite_wait.key(SLUG))) == ["t2"]


@pytest.mark.feature("swarm-launch-bounded-reads")
def test_failed_panes_held_reads_every_pane(store: RedisStore) -> None:
    failed_panes.hold(store, SLUG, _agent("engineer@1-1", "t1", pane_id="p1"), 5, "boom")
    assert failed_panes.held(store, SLUG) == {
        "p1": {"agent": "engineer@1-1", "task": "t1", "failed_at": 5, "reason": "boom"}
    }


@pytest.mark.feature("swarm-launch-bounded-reads")
def test_failed_panes_report_pages_the_launches_hash(store: RedisStore) -> None:
    key = store.key(SLUG, "launches")
    store.redis.hset(key, mapping={"a": json.dumps({"pane_lines": ["x"]}), "b": json.dumps({"pane_lines": []})})
    assert failed_panes.report(store, SLUG) == [{"pane_lines": ["x"]}]


@pytest.mark.feature("swarm-launch-bounded-reads")
def test_remote_control_failures_reads_every_master(store: RedisStore) -> None:
    remote_control.record(store, SLUG, "master@1-1", "lost", 7)
    assert remote_control.failures(store, SLUG) == {"master@1-1": {"message": "lost", "at": 7}}


@pytest.mark.feature("swarm-launch-bounded-reads")
def test_dispatch_seat_red_dev_reads_only_blocked_tasks(store: RedisStore) -> None:
    store.redis.hset(dev_red.key(SLUG), mapping={"t1": "5", "t2": "9", "t3": "11"})
    doc = {"tasks": [{"id": "t1", "state": "blocked"}, {"id": "t2", "state": "blocked"}, {"id": "t3", "state": "open"}]}
    assert dispatch_seat.red_dev(store, SLUG, doc) == [
        {"id": "dev-red:9", "kind": "freeze", "run": 9, "text": "t1, t2"}
    ]
    assert dispatch_seat.red_dev(store, SLUG, {"tasks": [{"id": "t3", "state": "open"}]}) == []


@pytest.mark.feature("swarm-launch-bounded-reads")
def test_run_window_reads_its_marks_by_field(store: RedisStore) -> None:
    window = {"start_ms": 0, "minutes": 10}
    config = replace(store.config(SLUG), window=window)
    key, bounds = store.key(SLUG, run_window.KEY), json.dumps([0, 10 * run_window.MINUTE_MS])
    store.redis.hset(key, mapping={"window": bounds, "mode": "enforce", "alerted": "1"})
    assert run_window.claims_stopped(store, SLUG, config, 10 * run_window.MINUTE_MS) != ""
    assert run_window.step(SLUG, config, store, None, {}, 5 * run_window.MINUTE_MS) == []
    assert store.redis.hget(key, "alerted") == "1"
    assert set(hash_pages.fields(store.redis, key)) <= set(run_window.MARKS)


@pytest.mark.feature("swarm-launch-bounded-reads")
def test_dispatch_seat_wake_reads_sent_triggers_by_id(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, str]] = []
    inbox = SimpleNamespace(send=lambda sender, address, text: sent.append((address, text)))
    monkeypatch.setattr(dispatch_seat.inbox_store, "InboxStore", lambda redis: inbox)
    store.redis.hset(store.key(SLUG, dispatch_seat.SENT), mapping={"t1": "{}", "old": "{}"})
    seat = _agent("dispatcher@1-1", "", seat="dispatcher@sw")
    found = [
        {"id": "t1", "kind": "priority", "item": "tasks/t1", "minutes": 15, "text": "a"},
        {"id": "t2", "kind": "priority", "item": "tasks/t2", "minutes": 15, "text": "b"},
    ]
    assert dispatch_seat._wake(SLUG, store, seat, found) == ["woke dispatcher@1-1 with 1 new trigger"]
    assert [address for address, _ in sent] == ["dispatcher@sw"]
    assert sorted(hash_pages.fields(store.redis, store.key(SLUG, dispatch_seat.SENT))) == ["old", "t1", "t2"]


@pytest.mark.feature("swarm-launch-bounded-reads")
def test_dispatch_seat_departed_seat_holds_only_sent_triggers(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.redis.hset(store.key(SLUG, dispatch_seat.SENT), mapping={"t1": "{}", "old": "{}"})
    store.put_agent(SLUG, replace(_agent("dispatcher@1-1", ""), lane=dispatch_seat.LANE))
    found = [{"id": "t1", "kind": "freeze"}, {"id": "t2", "kind": "freeze"}, {"id": "i1", "kind": "inbox"}]
    monkeypatch.setattr(dispatch_seat, "open_triggers", lambda *args, **kwargs: found)
    monkeypatch.setattr(dispatch_seat, "left", lambda seat, doc: True)
    assert dispatch_seat.run(SLUG, store.config(SLUG), store, None, {}, 1) == [
        f"ended dispatcher dispatcher@1-1: {dispatch_seat.DEPARTED}"
    ]
    assert store.redis.smembers(store.key(SLUG, dispatch_seat.LEFT)) == {"t1"}
