"""Regression tests: a reclaimed task's successor inherits the leaver's seat and mail, and a lost launch report never pins a later life."""

import json
from dataclasses import asdict, replace
from types import SimpleNamespace
from typing import Any, cast

import pytest

from scripts.events import memory
from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.hub import claims, lifecycle
from scripts.inbox.store import InboxStore
from scripts.swarm.store import AgentRecord, RedisStore
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


@pytest.mark.feature("hub-started-clears-launch-assignment")
def test_a_late_start_of_a_released_life_keeps_the_assignment_saved_for_the_next_life(rig: SimpleNamespace) -> None:
    admitted = rig.admitter.admit(SLUG, ask())
    rig.store.release(SLUG, "t1", admitted["name"])
    rig.store.redis.hset(rig.store.key(SLUG, ASSIGNMENTS), "t1", '{"seat": "eng-2@sw"}')

    claims.Claims(rig.store).handle(started(admitted))

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
    generation = rig.rows["t1"]["claim_generation"]
    assert rig.admitter.admit(SLUG, ask(generation=generation))["task"]["seat"] == "eng-2@sw"


@pytest.mark.feature("swarm-reclaim-keeps-seat")
@pytest.mark.parametrize(("done", "seat"), [(False, "eng-2"), (True, "")])
def test_a_hub_exit_keeps_the_leavers_seat_only_when_it_reopens_the_task(done: bool, seat: str) -> None:
    import fakeredis

    bus = memory.open_bus(BusConfig())
    agent = AgentRecord(name="engineer", lane="eng", task="task", execution_id="execution", generation=3, seat="eng-2")
    row = {"id": "task", "state": "claimed", "claimed_by": agent.name, "claim_generation": 7, "done": done}

    def task(slug: str, task_id: str) -> dict[str, object]:
        return dict(row)

    def update(slug: str, task_id: str, fields: dict[str, object], **guards: object) -> dict[str, object]:
        row.update(fields)
        return dict(row)

    def release(store: RedisStore, slug: str, record: AgentRecord) -> None:
        del store, slug, record

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.redis.hset(store.key("rig", "executions"), agent.execution_id, json.dumps(asdict(agent)))
    store.redis.hset(store.key("rig", "agents"), agent.name, json.dumps(asdict(agent)))
    store.redis.set(store.key("rig", "claim", agent.task), agent.name)
    bus.state("spoke-work").create("claim", json.dumps({"state": "started", "claim_revision": 1}).encode())
    data = {"name": agent.name, "task": "task", "claim_key": "claim", "claim_revision": 1, "task_generation": 7}
    event = Envelope("exited", "execution.exited", "rig", "execution", "execution", 3, 100, "hive", data)
    ledger = SimpleNamespace(task=task, update_task=update)

    lifecycle.Lifecycle(cast(Any, bus), store, cast(Any, ledger), release=release).handle(event)
    bus.close()

    assert store.handoff_seat("rig", "task") == seat
