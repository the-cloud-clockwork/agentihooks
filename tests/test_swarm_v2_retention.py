import json

import pytest

from scripts.swarm.store import SwarmError
from scripts.swarm_v2 import retention
from scripts.swarm_v2.retention import Final
from tests import sv2_kub04_cases as cases

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

SLUG = cases.SLUG


@pytest.fixture
def attempt():
    world = cases.World()
    with world.clocked():
        controller, _ = world.controller()
        assert controller.acquire()
        world.require = controller.require
        yield world, world.admit(controller)


def test_final_states_are_completed_and_cancelled():
    assert retention.FINAL_STATES == ("completed", "cancelled")
    assert retention.BACKLOG == ("waiting_outcome", "waiting_archive", "ready")


@pytest.mark.parametrize("state", ["completed", "cancelled"])
def test_finalize_takes_the_generation_from_the_execution_registry(attempt, state):
    world, record = attempt
    entry = retention.finalize(world.store, SLUG, world.require, record.execution_id, state, 10, "ledger:outcome")
    assert entry == Final(record.execution_id, record.generation, state, 10, "ledger:outcome")
    assert retention.final(world.store, SLUG, record.execution_id) == entry


def test_an_unrecorded_attempt_has_no_final_record(attempt):
    world, record = attempt
    assert retention.final(world.store, SLUG, record.execution_id) is None


@pytest.mark.parametrize("state", ["running", "failed", "", "Completed"])
def test_finalize_refuses_states_that_are_not_final(attempt, state):
    world, record = attempt
    with pytest.raises(SwarmError) as raised:
        retention.finalize(world.store, SLUG, world.require, record.execution_id, state, 10)
    assert str(raised.value) == "an attempt is final only when completed or explicitly cancelled"
    assert retention.final(world.store, SLUG, record.execution_id) is None


@pytest.mark.parametrize("end", [-1, 1.5, "10", True])
def test_finalize_refuses_a_transcript_end_that_is_not_an_offset(attempt, end):
    world, record = attempt
    with pytest.raises(SwarmError) as raised:
        retention.finalize(world.store, SLUG, world.require, record.execution_id, "completed", end)
    assert str(raised.value) == "the transcript end must be a non negative offset"


def test_finalize_accepts_a_zero_transcript_end(attempt):
    world, record = attempt
    assert retention.finalize(world.store, SLUG, world.require, record.execution_id, "completed", 0).transcript_end == 0


def test_finalize_refuses_an_unknown_execution(attempt):
    world, _ = attempt
    with pytest.raises(SwarmError) as raised:
        retention.finalize(world.store, SLUG, world.require, "exe-" + "0" * 32, "completed", 1)
    assert str(raised.value) == "unknown execution identity; display labels cannot identify attempts"


def test_finalize_writes_nothing_without_controller_authority(attempt):
    world, record = attempt

    def stale():
        raise SwarmError("the controller lease is stale")

    with pytest.raises(SwarmError) as raised:
        retention.finalize(world.store, SLUG, stale, record.execution_id, "completed", 10, "ledger:one")
    assert str(raised.value) == "the controller lease is stale"
    assert retention.final(world.store, SLUG, record.execution_id) is None


def test_the_outcome_reference_may_arrive_later_but_never_change(attempt):
    world, record = attempt
    retention.finalize(world.store, SLUG, world.require, record.execution_id, "completed", 10)
    later = retention.finalize(world.store, SLUG, world.require, record.execution_id, "completed", 10, "ledger:one")
    assert later.outcome_ref == "ledger:one"
    kept = retention.finalize(world.store, SLUG, world.require, record.execution_id, "completed", 10)
    assert kept.outcome_ref == "ledger:one"
    with pytest.raises(SwarmError) as raised:
        retention.finalize(world.store, SLUG, world.require, record.execution_id, "completed", 10, "ledger:two")
    assert str(raised.value) == "a final attempt record cannot change"
    assert retention.final(world.store, SLUG, record.execution_id).outcome_ref == "ledger:one"


@pytest.mark.parametrize("change", [("cancelled", 10), ("completed", 11), ("completed", 9)])
def test_state_and_transcript_end_are_fixed_once_recorded(attempt, change):
    world, record = attempt
    retention.finalize(world.store, SLUG, world.require, record.execution_id, "completed", 10)
    with pytest.raises(SwarmError) as raised:
        retention.finalize(world.store, SLUG, world.require, record.execution_id, *change)
    assert str(raised.value) == "a final attempt record cannot change"


def test_archive_watermark_reads_the_accepted_heartbeat_and_defaults_to_zero(attempt):
    world, record = attempt
    assert retention.archive_watermark(world.store, SLUG, record.execution_id) == 0
    world.acknowledge(record.execution_id, 77)
    assert retention.archive_watermark(world.store, SLUG, record.execution_id) == 77


def test_waiting_names_the_missing_outcome_before_the_archive(attempt):
    world, record = attempt
    entry = retention.finalize(world.store, SLUG, world.require, record.execution_id, "completed", 10)
    assert retention.waiting(world.store, SLUG, entry) == "waiting_outcome"
    entry = retention.finalize(world.store, SLUG, world.require, record.execution_id, "completed", 10, "ledger:one")
    world.acknowledge(record.execution_id, 9)
    assert retention.waiting(world.store, SLUG, entry) == "waiting_archive"
    world.acknowledge(record.execution_id, 10)
    assert retention.waiting(world.store, SLUG, entry) == ""
    world.acknowledge(record.execution_id, 11)
    assert retention.waiting(world.store, SLUG, entry) == ""


def test_retain_keeps_the_execution_record_archive_watermark_and_removal(attempt):
    world, record = attempt
    entry = retention.finalize(world.store, SLUG, world.require, record.execution_id, "cancelled", 5, "ledger:one")
    world.acknowledge(record.execution_id, 5)
    removed = {"pods": [{"name": "p", "uid": "u", "outcome": "deleted"}], "services": []}
    kept = retention.retain(world.store, SLUG, entry, removed)
    assert kept == {
        "execution": json.loads(world.store.redis.hget(world.store.key(SLUG, "executions"), record.execution_id)),
        "final": {
            "execution_id": record.execution_id,
            "generation": record.generation,
            "state": "cancelled",
            "transcript_end": 5,
            "outcome_ref": "ledger:one",
        },
        "archive_watermark": 5,
        "removed": removed,
        "retained_at_ms": world.clock[0],
    }
    assert retention.retained(world.store, SLUG, record.execution_id) == kept
    assert world.store.execution(SLUG, record.execution_id) == record


def test_retain_reads_the_clock_of_its_own_store(attempt, monkeypatch):
    world, record = attempt
    entry = retention.finalize(world.store, SLUG, world.require, record.execution_id, "completed", 0, "ledger:one")
    seen = []
    monkeypatch.setattr(retention.lease, "now_ms", lambda store: seen.append(store) or 7)
    assert retention.retain(world.store, SLUG, entry, {})["retained_at_ms"] == 7
    assert seen and all(store is world.store for store in seen)


def test_nothing_is_retained_before_cleanup(attempt):
    world, record = attempt
    assert retention.retained(world.store, SLUG, record.execution_id) is None


def test_backlog_counts_final_attempts_by_reason_until_retained():
    world = cases.World()
    with world.clocked():
        controller, _ = world.controller()
        assert controller.acquire()
        world.require = controller.require
        records = [world.admit(controller, seat=f"eng-{n}") for n in range(1, 5)]
        assert retention.execution_cleanup_backlog(world.store, SLUG) == {
            "waiting_outcome": 0,
            "waiting_archive": 0,
            "ready": 0,
        }
        entries = [
            retention.finalize(world.store, SLUG, world.require, records[0].execution_id, "completed", 10),
            retention.finalize(world.store, SLUG, world.require, records[1].execution_id, "completed", 10, "o"),
            retention.finalize(world.store, SLUG, world.require, records[2].execution_id, "cancelled", 0, "o"),
            retention.finalize(world.store, SLUG, world.require, records[3].execution_id, "completed", 3, "o"),
        ]
        world.acknowledge(records[3].execution_id, 3)
        assert retention.execution_cleanup_backlog(world.store, SLUG) == {
            "waiting_outcome": 1,
            "waiting_archive": 1,
            "ready": 2,
        }
        retention.retain(world.store, SLUG, entries[3], {"pods": [], "services": []})
        assert retention.execution_cleanup_backlog(world.store, SLUG) == {
            "waiting_outcome": 1,
            "waiting_archive": 1,
            "ready": 1,
        }
