import json
from dataclasses import asdict

import pytest

from scripts.swarm.status import handoff_rows, status_report
from scripts.swarm_v2.checkpoints import cards

pytestmark = pytest.mark.unit


@pytest.fixture
def world():
    from tests import sv2_kub05_cases as cases

    world = cases.World()
    with world.clocked():
        controller, _ = world.controller()
        assert controller.acquire()
        world.live = controller
        world.old = world.launch(controller, world.fx["first"])
        yield world


@pytest.mark.parametrize("continuation,label", [("native", "Resume native"), ("context", "Continue from context")])
def test_a_real_attempt_failure_card_offers_its_verified_checkpoint(world, continuation, label):
    recovery = world.recovery(world.live, automatic=False)
    world.checkpoints.by_execution[world.old.execution_id] = [
        {
            "checkpoint_id": "ckp-verified",
            "sequence": 1,
            "status": "complete",
            "compatibility": world.fx["compatibility"],
            "continuation": continuation,
            "watermark": 41,
        }
    ]
    recovery.handle(world.old.execution_id, "node_lost")
    source = cards.rows(world.store, recovery.slug)
    [row] = handoff_rows([], [asdict(world.occupant())], source)
    assert row["awaiting"] == world.old.name
    assert row["resume_available"] is True
    assert row["resume_label"] == label
    assert row["checkpoint"] == "ckp-verified"
    [reported] = status_report(world.store, recovery.slug, {})["handoffs"]
    assert reported["checkpoint"] == "ckp-verified"
    assert reported["resume_label"] == label
    mode = "native resume" if continuation == "native" else "context continuation"
    assert row["reason"] == f"node_lost; {mode} from ckp-verified at record 41"
    fence = json.loads(world.store.redis.hget(world.store.key(recovery.slug, "attempt-fences"), world.old.execution_id))
    assert source == {
        world.old.seat: {
            "generation": world.old.generation,
            "at": fence["fenced_at_ms"],
            "reason": row["reason"],
            "checkpoint": "ckp-verified",
            "resume_available": True,
            "resume_label": label,
            "continuity": "pending",
        }
    }
    assert world.store.restored(recovery.slug) == []


def test_card_reports_verified_native_continuity_after_worker_receipt(world):
    recovery = world.recovery(world.live)
    decision = recovery.handle(world.old.execution_id, "node_lost")
    directive = recovery.restore(decision.replacement)
    recovery.confirm(decision.replacement, {**directive, "command": ["claude", "--resume", directive["session_id"]]})
    source = cards.rows(world.store, recovery.slug)
    assert source[world.old.seat]["continuity"] == "native"


def test_card_keeps_newest_failure_per_seat_and_continues_after_old_records(world):
    recovery = world.recovery(world.live, automatic=False)
    recovery.handle(world.old.execution_id, "node_lost")
    redis, key = world.store.redis, lambda suffix: world.store.key(recovery.slug, suffix)
    original = world.old.execution_id
    decision = json.loads(redis.hget(key("attempt-recoveries"), original))
    fence = json.loads(redis.hget(key("attempt-fences"), original))
    redis.hdel(key("attempt-recoveries"), original)
    redis.hset(
        key("attempt-recoveries"),
        "newest",
        json.dumps({**decision, "generation": decision["generation"] + 1, "reason": "newest"}),
    )
    redis.hset(key("attempt-fences"), "newest", json.dumps(fence))
    redis.hset(key("attempt-recoveries"), original, json.dumps(decision))
    redis.hset(key("attempt-recoveries"), "other", json.dumps({**decision, "reason": "other"}))
    redis.hset(key("attempt-fences"), "other", json.dumps({**fence, "seat": "other-seat"}))
    source = cards.rows(world.store, recovery.slug)
    assert set(source) == {world.old.seat, "other-seat"}
    assert source[world.old.seat]["reason"] == "newest"
    assert source[world.old.seat]["generation"] == decision["generation"] + 1
    assert source["other-seat"]["reason"] == "other"


def test_a_card_with_no_checkpoint_offers_only_fresh_without_rewriting_legacy_restore_rows(world):
    recovery = world.recovery(world.live, automatic=False)
    world.checkpoints.by_execution[world.old.execution_id] = []
    world.store.put_restored(recovery.slug, [{"name": "legacy", "outcome": "awaiting-decision"}])
    recovery.handle(world.old.execution_id, "node_lost")
    [row] = handoff_rows([], [asdict(world.occupant())], cards.rows(world.store, recovery.slug))
    assert row["resume_available"] is False
    assert row["checkpoint"] == ""
    assert world.store.restored(recovery.slug) == [{"name": "legacy", "outcome": "awaiting-decision"}]
