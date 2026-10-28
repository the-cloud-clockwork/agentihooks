from dataclasses import replace

import pytest

from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.events.memory import open_bus
from scripts.hub import claims, registry
from scripts.swarm.store import AgentRecord
from tests.hub.test_admission import SLUG, ask
from tests.hub.test_admission import rig as rig


def started(admitted):
    task = admitted["task"]
    return Envelope(
        id=f"started:{task['execution_id']}",
        type="execution.started",
        swarm=SLUG,
        entity="execution",
        key=task["execution_id"],
        version=task["execution_generation"],
        time=7_000,
        author="hive-a",
        data={"task": "t1", "name": admitted["name"], "claim_key": "claim", "hive": "hive-a"},
    )


def test_started_execution_counts_one_life_across_redelivery_and_reactor_restart(rig):
    event = started(rig.admitter.admit(SLUG, ask()))
    claims.Claims(rig.store).handle(event)
    claims.Claims(rig.store).handle(replace(event, id="redelivery"))

    assert rig.store.claims(SLUG, "t1") == 1
    rig.store.reset_claims(SLUG, "t1")
    claims.Claims(rig.store).handle(event)
    assert rig.store.claims(SLUG, "t1") == 0


def test_a_counted_start_reports_its_life_on_stderr(rig, capsys):
    admitted = rig.admitter.admit(SLUG, ask())
    capsys.readouterr()
    claims.Claims(rig.store).handle(started(admitted))

    execution = admitted["task"]["execution_id"]
    assert capsys.readouterr() == ("", f"hub: claim life swarm={SLUG} task=t1 execution={execution} lives=1\n")


@pytest.mark.parametrize("state, lives", [("started", 0), ("failed", 1)])
def test_a_start_the_legacy_tick_already_counted_spends_no_second_life(rig, state, lives):
    admitted = rig.admitter.admit(SLUG, ask())
    rig.store.record_launch(SLUG, AgentRecord(admitted["name"], "eng", "t1"), state)

    claims.Claims(rig.store).handle(started(admitted))

    assert rig.store.claims(SLUG, "t1") == lives


def test_reset_clears_the_pending_life(rig):
    rig.admitter.admit(SLUG, ask())
    assert rig.store.redis.hget(rig.store.key(SLUG, "pending-lives"), "t1") is not None

    rig.store.reset_claims(SLUG, "t1")

    assert rig.store.redis.hget(rig.store.key(SLUG, "pending-lives"), "t1") is None


def test_distinct_execution_generations_spend_distinct_lives(rig):
    event = started(rig.admitter.admit(SLUG, ask()))
    counter = claims.Claims(rig.store)
    counter.handle(event)
    counter.handle(replace(event, version=event.version + 1))
    counter.handle(replace(event, key="another-execution"))

    assert rig.store.claims(SLUG, "t1") == 3


def test_failed_launches_and_events_without_spoke_claims_spend_no_life(rig):
    event = started(rig.admitter.admit(SLUG, ask()))
    counter = claims.Claims(rig.store)
    counter.handle(replace(event, type="execution.exited"))
    counter.handle(replace(event, entity="task"))
    counter.handle(replace(event, data={**event.data, "claim_key": ""}))

    assert rig.store.claims(SLUG, "t1") == 0


def test_default_hub_registers_the_started_life_counter():
    bus = open_bus(BusConfig())
    try:
        reactor = next(item for item in registry.configured(bus) if item.name == "claim-lives")
        assert reactor.subject == "swarm.*.execution.started"
        assert reactor.singleton
    finally:
        bus.close()


def test_a_delayed_start_cannot_spend_a_reset_budget(rig):
    event = started(rig.admitter.admit(SLUG, ask()))
    rig.store.reset_claims(SLUG, "t1")
    claims.Claims(rig.store).handle(event)
    assert rig.store.claims(SLUG, "t1") == 0


def test_a_pending_start_refund_preserves_earlier_lives(rig):
    for _ in range(2):
        rig.store.count_claim(SLUG, "t1")
    admitted = rig.admitter.admit(SLUG, ask())
    event = started(admitted)
    assert rig.store.refund_held_claim(SLUG, "t1", admitted["name"], rig.store.claim_generation(SLUG, "t1"))
    assert rig.store.claims(SLUG, "t1") == 2
    claims.Claims(rig.store).handle(event)
    assert rig.store.claims(SLUG, "t1") == 2


def test_the_first_pending_start_can_be_refunded(rig):
    admitted = rig.admitter.admit(SLUG, ask())
    assert rig.store.refund_held_claim(SLUG, "t1", admitted["name"], rig.store.claim_generation(SLUG, "t1"))
    claims.Claims(rig.store).handle(started(admitted))
    assert rig.store.claims(SLUG, "t1") == 0


def test_a_reopened_admission_spends_no_life_and_a_counted_start_refunds_normally(rig):
    rig.store.count_claim(SLUG, "t1")
    reopened = rig.admitter.admit(SLUG, ask())
    assert rig.admitter.reopen(SLUG, ask())
    claims.Claims(rig.store).handle(started(reopened))
    assert rig.store.claims(SLUG, "t1") == 1

    admitted = rig.admitter.admit(SLUG, ask(generation=2))
    claims.Claims(rig.store).handle(started(admitted))
    assert rig.store.claims(SLUG, "t1") == 2
    assert rig.store.refund_held_claim(SLUG, "t1", admitted["name"], rig.store.claim_generation(SLUG, "t1"))
    assert rig.store.claims(SLUG, "t1") == 1


def test_a_new_claim_drops_a_stale_pending_life_so_its_refund_spends_a_counted_life(rig):
    rig.admitter.admit(SLUG, ask())
    rig.store.release(SLUG, "t1", rig.store.claimant(SLUG, "t1"))
    assert rig.store.claim(SLUG, "t1", "engineer@legacy", 60_000)
    rig.store.count_claim(SLUG, "t1")

    assert rig.store.refund_held_claim(SLUG, "t1", "engineer@legacy", rig.store.claim_generation(SLUG, "t1"))
    assert rig.store.claims(SLUG, "t1") == 0
