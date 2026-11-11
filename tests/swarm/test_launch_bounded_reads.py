"""Each launch path read runs keyed or paged on a store that refuses a whole hash read."""

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import fakeredis
import pytest

from scripts.swarm import (
    dev_red,
    dispatch_seat,
    failed_panes,
    hash_pages,
    launch_check,
    launch_job,
    local_slots,
    preempt,
    prerequisite_wait,
    remote_control,
    run_window,
)
from scripts.swarm import tick as swarm_tick
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.tick.steps import place

SLUG = "sw"
FEATURE = pytest.mark.feature("swarm-launch-bounded-reads")


class Refusing(fakeredis.FakeRedis):
    """A Redis double whose whole hash read fails the test."""

    def hgetall(self, name: Any) -> Any:
        """Refuse every whole hash read."""
        raise AssertionError(f"whole hash read of {name}")


@pytest.fixture
def store() -> RedisStore:
    """A swarm store on a Redis double that refuses whole hash reads."""
    created = RedisStore(Refusing(decode_responses=True))
    created.create(SwarmConfig(SLUG, "/repo", max_eng=2, max_ci=1))
    return created


def _agent(name: str, task: str, **fields: Any) -> AgentRecord:
    return AgentRecord(name=name, task=task, lane="eng", pane_id="", started_at=1, **fields)


@FEATURE
def test_fields_reads_every_field_across_pages(store: RedisStore) -> None:
    store.redis.hset("h", mapping={f"f{n}": str(n) for n in range(7)})
    assert hash_pages.fields(store.redis, "h", page=2) == {f"f{n}": str(n) for n in range(7)}
    assert hash_pages.fields(store.redis, "missing") == {}


@FEATURE
def test_picked_returns_only_the_named_fields_that_are_set(store: RedisStore) -> None:
    store.redis.hset("h", mapping={"a": "1", "b": "2", "c": "3"})
    assert hash_pages.picked(store.redis, "h", ["a", "c", "z", "a"]) == {"a": "1", "c": "3"}
    assert hash_pages.picked(store.redis, "h", []) == {}


@FEATURE
def test_place_holds_spawns_for_another_swarms_waiting_master(store: RedisStore) -> None:
    store.redis.hset(swarm_tick.MASTER_WAITING, mapping={SLUG: "100", "other": "100", "old": "-999999999"})
    assert place._held_for_master(SLUG, store, 200) == [
        "holding spawns: swarm other waits on a session slot for its master"
    ]


@FEATURE
def test_launch_check_reads_pending_checks_and_reports_paged(store: RedisStore) -> None:
    launch_check.begin(store, SLUG, _agent("engineer@1-1", "t1"), 10)
    held = _agent("engineer@1-2", "t2")
    launch_check.record(store, SLUG, held, {"profile": {"expected": "a", "actual": "b"}}, 20, 5, held=True)
    launch_check.record(store, SLUG, _agent("engineer@1-3", "t3"), {}, 30, 5)
    assert launch_check.pending(store, SLUG) == {"engineer@1-1": {"task": "t1", "at": 10, "relaunch": True}}
    assert sorted(found["agent"] for found in launch_check.reports(store, SLUG)) == ["engineer@1-2", "engineer@1-3"]
    assert launch_check.judged(store, SLUG) == {"engineer@1-1", "engineer@1-2"}
    assert [finding.subject.split("/")[0] for finding in launch_check.findings(store, SLUG)] == ["profile"]


@FEATURE
def test_launch_job_collect_drops_a_job_whose_agent_is_gone(store: RedisStore) -> None:
    key = store.key(SLUG, launch_job.KEY)
    store.redis.hset(key, "engineer@1-9", json.dumps({"task": "t9", "placed": {}}))
    run = SimpleNamespace(store=store, slug=SLUG)
    assert launch_job.collect(run) == ([], set())
    assert not store.redis.exists(key)


@FEATURE
def test_local_slots_count_and_claim_held_slots_paged(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_slots, "live", lambda: set())
    now = local_slots.lease.now_ms(store)
    store.redis.hset(local_slots.key("m"), mapping={f"{SLUG}/a": str(now), f"{SLUG}/b": str(now - 10**9)})
    assert local_slots.held(store, "m", SLUG, "t1") == 1
    assert local_slots.occupancy(store, "m") == (0, 1)
    assert local_slots.claim(store, "m", SLUG, "t1", "c", 5) == 1
    assert sorted(store.redis.hkeys(local_slots.key("m"))) == [f"{SLUG}/a", f"{SLUG}/c"]


@FEATURE
def test_preempt_keeps_only_asks_of_live_holders(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scripts.hive.placement_waits.read", lambda store, slug: {})
    key = store.key(SLUG, preempt.ASKED)
    store.redis.hset(key, mapping={"engineer@1-1": json.dumps(["t9", 1]), "engineer@1-2": json.dumps(["t8", 1])})
    full = {"eng": ([], [_agent("engineer@1-1", "t1")])}
    assert preempt.step(SLUG, store, {}, {"tasks": []}, full, 2) == []
    assert store.redis.hkeys(key) == ["engineer@1-1"]


@FEATURE
def test_prerequisite_wait_reopen_pass_drops_a_stale_blocked_record(store: RedisStore) -> None:
    record = {"agent": "engineer@1-1", "generation": 1, "target": "x", "blocked": True}
    store.redis.hset(prerequisite_wait.key(SLUG), mapping={"t1": json.dumps(record), "t2": json.dumps({})})
    assert prerequisite_wait.reopen_pass(SLUG, store, None, {"t1": {"state": "open"}}) == []
    assert store.redis.hkeys(prerequisite_wait.key(SLUG)) == ["t2"]


@FEATURE
def test_failed_panes_held_reads_every_pane(store: RedisStore) -> None:
    failed_panes.hold(store, SLUG, _agent("engineer@1-1", "t1", pane_id="p1"), 5, "boom")
    assert failed_panes.held(store, SLUG) == {
        "p1": {"agent": "engineer@1-1", "task": "t1", "failed_at": 5, "reason": "boom"}
    }


@FEATURE
def test_remote_control_failures_reads_every_master(store: RedisStore) -> None:
    remote_control.record(store, SLUG, "master@1-1", "lost", 7)
    assert remote_control.failures(store, SLUG) == {"master@1-1": {"message": "lost", "at": 7}}


@FEATURE
def test_dispatch_seat_red_dev_reads_only_blocked_tasks(store: RedisStore) -> None:
    store.redis.hset(dev_red.key(SLUG), mapping={"t1": "5", "t2": "9", "t3": "11"})
    doc = {"tasks": [{"id": "t1", "state": "blocked"}, {"id": "t2", "state": "blocked"}, {"id": "t3", "state": "open"}]}
    assert dispatch_seat.red_dev(store, SLUG, doc) == [
        {"id": "dev-red:9", "kind": "freeze", "run": 9, "text": "t1, t2"}
    ]
    assert dispatch_seat.red_dev(store, SLUG, {"tasks": [{"id": "t3", "state": "open"}]}) == []


@FEATURE
def test_run_window_reads_its_marks_by_field(store: RedisStore) -> None:
    window = {"start_ms": 0, "minutes": 10}
    config = replace(store.config(SLUG), window=window)
    key, bounds = store.key(SLUG, run_window.KEY), json.dumps([0, 10 * run_window.MINUTE_MS])
    store.redis.hset(key, mapping={"window": bounds, "mode": "enforce", "alerted": "1", "stray": "x"})
    assert run_window.claims_stopped(store, SLUG, config, 10 * run_window.MINUTE_MS) != ""
    assert run_window.step(SLUG, config, store, None, {}, 5 * run_window.MINUTE_MS) == []
    assert store.redis.hget(key, "alerted") == "1"
    assert store.redis.hget(key, "stray") is None
