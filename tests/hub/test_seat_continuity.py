"""Regression tests: a reclaimed task's successor inherits the leaver's seat and mail, and a lost launch report never pins a later life."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from scripts.hub import claims
from scripts.inbox.store import InboxStore
from scripts.swarm.store import AgentRecord
from scripts.tick.steps import common
from tests.hub.test_admission import SLUG, ask
from tests.hub.test_admission import rig as rig
from tests.hub.test_claim_lives import started

ASSIGNMENTS = "launch-assignments"


@pytest.mark.feature("hub-started-clears-launch-assignment")
def test_a_durable_start_clears_the_saved_launch_assignment_when_the_launch_report_is_lost(
    rig: SimpleNamespace,
) -> None:
    rig.store.redis.hset(rig.store.key(SLUG, ASSIGNMENTS), "t1", '{"seat": "eng-3@sw", "account": "stale"}')
    admitted = rig.admitter.admit(SLUG, ask())

    claims.Claims(rig.store).handle(started(admitted))

    assert rig.store.redis.hget(rig.store.key(SLUG, ASSIGNMENTS), "t1") is None


@pytest.mark.feature("hub-started-clears-launch-assignment")
def test_a_redelivered_start_of_a_counted_life_keeps_a_later_saved_assignment(rig: SimpleNamespace) -> None:
    event = started(rig.admitter.admit(SLUG, ask()))
    claims.Claims(rig.store).handle(event)
    rig.store.redis.hset(rig.store.key(SLUG, ASSIGNMENTS), "t1", '{"seat": "eng-2@sw"}')

    claims.Claims(rig.store).handle(replace(event, id="redelivery"))

    assert rig.store.redis.hget(rig.store.key(SLUG, ASSIGNMENTS), "t1") == '{"seat": "eng-2@sw"}'


@pytest.mark.feature("swarm-reclaim-keeps-seat")
def test_a_reclaimed_tasks_successor_takes_the_leavers_seat_and_its_mail(rig: SimpleNamespace) -> None:
    rig.store.put_agent(SLUG, AgentRecord("engineer@neighbour", "eng", "t9", seat="eng-1@sw"))
    admitted = rig.admitter.admit(SLUG, ask())
    [leaver] = [agent for agent in rig.store.agents(SLUG) if agent.name == admitted["name"]]
    assert leaver.seat == "eng-2@sw"
    InboxStore(rig.store.redis).send("operator", leaver.name, "keep the proof", task="t1")

    assert common._drop(SLUG, rig.store, rig.ledger, rig.rows, leaver) == ", task t1 reopened"
    rig.store.drop_agent(SLUG, "engineer@neighbour")

    assert rig.store.handoff_seat(SLUG, "t1") == "eng-2@sw"
    assert [item.text for item in InboxStore(rig.store.redis).inbox("eng-2@sw")] == ["keep the proof"]
    assert rig.admitter.admit(SLUG, ask(generation=1))["task"]["seat"] == "eng-2@sw"
