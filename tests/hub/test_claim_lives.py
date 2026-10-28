from dataclasses import replace

from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.events.memory import open_bus
from scripts.hub import claims, registry
from tests.hub.test_admission import SLUG, ask, rig as rig


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
        reactor = registry.configured(bus).get("claim-lives")
        assert reactor.subject == "swarm.*.execution.started"
        assert reactor.singleton
    finally:
        bus.close()
