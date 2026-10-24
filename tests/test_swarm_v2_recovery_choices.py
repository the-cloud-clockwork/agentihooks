import json

import pytest

from scripts.swarm.store import SwarmError
from scripts.swarm_v2.kubernetes import failures
from tests import sv2_kub05_cases as cases

pytestmark = pytest.mark.unit


@pytest.fixture
def world():
    world = cases.World()
    with world.clocked():
        controller, _ = world.controller()
        assert controller.acquire()
        world.live = controller
        world.old = world.launch(controller, world.fx["first"])
        yield world


def _entry(world, checkpoint_id, sequence, **changes):
    entry = {"checkpoint_id": checkpoint_id, "sequence": sequence, "status": "complete"}
    return {**entry, "compatibility": world.fx["compatibility"], "continuation": "native", **changes}


def _pending(world, entries):
    world.checkpoints.by_execution[world.old.execution_id] = entries
    recovery = world.recovery(world.live, automatic=False)
    assert recovery.handle(world.old.execution_id, "node_lost").mode == failures.PENDING
    return recovery


def _published(world):
    raw = world.store.redis.hget(world.store.key(cases.SLUG, failures.MATERIAL), world.old.execution_id)
    return json.loads(raw)


def test_choice_names_and_refusals_are_fixed():
    assert failures.CHOICES == ("native", "context", "fresh")
    assert (failures.NATIVE, failures.CONTEXT) == ("native", "context")
    assert failures.MATERIAL == "attempt-recovery-material"
    assert failures.REFUSED_REQUESTS == "recovery-request-refusals"
    assert failures.DECIDED == "attempt-decided-at"
    assert failures.NO_SESSION == "the lost attempt held no native session"
    assert failures.NO_NATIVE == "no complete compatible native checkpoint to resume from"
    assert failures.NO_CHECKPOINT == "no complete compatible checkpoint to resume from"


def test_native_is_refused_on_a_context_only_checkpoint_and_the_seat_keeps_waiting(world):
    recovery = _pending(world, [_entry(world, "ckpt-context", 1, continuation="context")])
    before = world.attempts()
    with pytest.raises(SwarmError, match=f"^{failures.NO_NATIVE}$"):
        recovery.decide(world.old.execution_id, "native")
    assert world.attempts() == before
    assert world.occupant().state == failures.AWAITING
    assert recovery.decision(world.old.execution_id).mode == failures.PENDING


def test_native_is_refused_without_a_native_session(world):
    run = {**world.fx["second"], "native_session": ""}
    other = world.launch(world.live, run, "eng-2")
    recovery = world.recovery(world.live, automatic=False)
    recovery.handle(other.execution_id, "oom_killed")
    with pytest.raises(SwarmError, match=f"^{failures.NO_SESSION}$"):
        recovery.decide(other.execution_id, "native")
    assert recovery.unavailable(world.store.execution(cases.SLUG, other.execution_id), "context") == ""


def test_context_is_refused_without_any_usable_checkpoint(world):
    recovery = _pending(world, [_entry(world, "ckpt-partial", 1, status="partial")])
    with pytest.raises(SwarmError, match=f"^{failures.NO_CHECKPOINT}$"):
        recovery.decide(world.old.execution_id, "context")
    assert recovery.unavailable(world.old, "fresh") == ""


def test_context_reconstructs_even_from_a_native_checkpoint(world):
    recovery = _pending(world, [_entry(world, "ckpt-native", 4)])
    world.clock[0] += 7000
    decision = recovery.decide(world.old.execution_id, "context")
    assert (decision.mode, decision.checkpoint) == ("context", "ckpt-native")
    assert recovery.material(world.old.execution_id)["decided_at_ms"] == world.clock[0]
    assert recovery.restore(decision.replacement)["recovery"] == "reconstructed"


def test_native_restores_natively_from_the_newest_native_checkpoint(world):
    entries = [_entry(world, "ckpt-native", 2), _entry(world, "ckpt-context", 3, continuation="context")]
    recovery = _pending(world, entries)
    decision = recovery.decide(world.old.execution_id, "native")
    restore = recovery.restore(decision.replacement)
    assert (decision.checkpoint, restore["recovery"]) == ("ckpt-native", "native")
    assert restore["session_id"] == world.fx["first"]["native_session"]


def test_an_unknown_choice_is_refused_before_any_read(world):
    recovery = _pending(world, [])
    with pytest.raises(SwarmError, match="^a recovery decision is native, context, fresh or resume$"):
        recovery.decide(world.old.execution_id, "recovery_pending")


def test_material_names_the_latest_checkpoint_the_passed_over_ones_and_each_choice(world):
    entries = [
        _entry(world, "ckpt-context", 1, continuation="context"),
        _entry(world, "ckpt-corrupt", 3, status="incomplete", reason="corrupt_object"),
        _entry(world, "ckpt-older-image", 4, compatibility="worker-image-older:claude"),
        _entry(world, "ckpt-partial", 2, status="partial"),
        _entry(world, "ckpt-unverified", 5, status=""),
        _entry(world, "ckpt-unrecorded", 6, compatibility=""),
    ]
    recovery = _pending(world, entries)
    shown = recovery.material(world.old.execution_id)
    fence = recovery.fence(world.old.execution_id)
    assert shown == _published(world)
    assert shown == {
        "execution_id": world.old.execution_id,
        "generation": world.old.generation,
        "agent": world.old.name,
        "seat": world.old.seat,
        "task": world.old.task,
        "reason": "node_lost",
        "fenced_at_ms": fence["fenced_at_ms"],
        "archive_watermark": world.fx["first"]["archive_watermark"],
        "tail": fence["tail"],
        "checkpoint": {"checkpoint_id": "ckpt-context", "sequence": 1, "continuation": "context"},
        "refused": [
            {
                "checkpoint_id": "ckpt-unrecorded",
                "sequence": 6,
                "diagnosis": f"written by an unrecorded image, this worker runs {world.fx['compatibility']}",
            },
            {"checkpoint_id": "ckpt-unverified", "sequence": 5, "diagnosis": "unverified"},
            {
                "checkpoint_id": "ckpt-older-image",
                "sequence": 4,
                "diagnosis": f"written by worker-image-older:claude, this worker runs {world.fx['compatibility']}",
            },
            {"checkpoint_id": "ckpt-corrupt", "sequence": 3, "diagnosis": "corrupt_object"},
            {"checkpoint_id": "ckpt-partial", "sequence": 2, "diagnosis": "partial"},
        ],
        "choices": {"native": failures.NO_NATIVE, "context": "", "fresh": ""},
        "decision": {
            "execution_id": world.old.execution_id,
            "generation": world.old.generation,
            "reason": "node_lost",
            "mode": failures.PENDING,
            "checkpoint": "",
            "replacement": "",
        },
        "decided_at_ms": 0,
        "requested": "",
        "refused_request": None,
    }


def test_material_is_absent_for_an_unfenced_attempt_and_lists_everything_without_a_checkpoint(world):
    recovery = world.recovery(world.live, automatic=False)
    assert recovery.material(world.old.execution_id) is None
    world.checkpoints.by_execution[world.old.execution_id] = [_entry(world, "ckpt-partial", 0, status="partial")]
    recovery.handle(world.old.execution_id, "evicted")
    shown = recovery.material(world.old.execution_id)
    assert shown["checkpoint"] is None
    assert shown["refused"] == [{"checkpoint_id": "ckpt-partial", "sequence": 0, "diagnosis": "partial"}]
    assert shown["choices"] == {"native": failures.NO_NATIVE, "context": failures.NO_CHECKPOINT, "fresh": ""}


def test_a_refused_request_is_recorded_for_the_page_and_the_seat_keeps_waiting(world):
    recovery = _pending(world, [_entry(world, "ckpt-context", 1, continuation="context")])
    requests = world.store.key(cases.SLUG, failures.REQUESTS)
    world.store.redis.hset(requests, world.old.execution_id, "native")
    world.reconcile(recovery)
    shown = _published(world)
    assert shown["refused_request"] == {"choice": "native", "reason": failures.NO_NATIVE}
    assert shown["requested"] == ""
    assert world.store.redis.hgetall(requests) == {}
    assert world.occupant().state == failures.AWAITING


def test_an_accepted_decision_clears_the_earlier_refused_request(world):
    recovery = _pending(world, [_entry(world, "ckpt-context", 1, continuation="context")])
    world.store.redis.hset(world.store.key(cases.SLUG, failures.REQUESTS), world.old.execution_id, "native")
    world.reconcile(recovery)
    recovery.decide(world.old.execution_id, "context")
    assert _published(world)["refused_request"] is None
    assert world.store.redis.hgetall(world.store.key(cases.SLUG, failures.REFUSED_REQUESTS)) == {}


def test_each_pass_republishes_pending_material_only(world):
    recovery = _pending(world, [])
    world.store.redis.delete(world.store.key(cases.SLUG, failures.MATERIAL))
    world.checkpoints.by_execution[world.old.execution_id] = [_entry(world, "ckpt-late", 1)]
    world.reconcile(recovery)
    assert _published(world)["checkpoint"]["checkpoint_id"] == "ckpt-late"
    decision = recovery.decide(world.old.execution_id, "fresh")
    assert _published(world)["decision"]["mode"] == "fresh"
    world.store.redis.delete(world.store.key(cases.SLUG, failures.MATERIAL))
    world.reconcile(recovery)
    assert world.store.redis.hget(world.store.key(cases.SLUG, failures.MATERIAL), decision.execution_id) is None


def test_a_repeated_native_or_context_request_reuses_the_accepted_decision(world):
    recovery = _pending(world, [_entry(world, "ckpt-native", 1)])
    first = recovery.request(world.old.execution_id, "native")
    attempts = world.attempts()
    assert recovery.request(world.old.execution_id, "native") == first
    assert world.attempts() == attempts
    with pytest.raises(SwarmError, match="^no recovery decision is pending for this attempt$"):
        recovery.request(world.old.execution_id, "context")


def test_an_automatic_replacement_records_when_it_was_decided(world):
    world.checkpoints.by_execution[world.old.execution_id] = [_entry(world, "ckpt-native", 1)]
    recovery = world.recovery(world.live)
    decision = recovery.handle(world.old.execution_id, "node_lost")
    assert decision == failures.Decision(
        world.old.execution_id, world.old.generation, "node_lost", "resume", "ckpt-native", decision.replacement
    )
    assert _published(world)["decided_at_ms"] == world.clock[0]


def test_published_material_is_stored_with_sorted_keys(world):
    recovery = _pending(world, [_entry(world, "ckpt-context", 1, continuation="context")])
    raw = world.store.redis.hget(world.store.key(cases.SLUG, failures.MATERIAL), world.old.execution_id)
    assert raw == json.dumps(recovery.material(world.old.execution_id), sort_keys=True)
