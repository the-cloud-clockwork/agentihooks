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
        data={"task": "t1", "name": admitted["name"], "claim_key": "claim", "hive": "hive-a", "pane_id": "w1:p3"},
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


def test_a_counted_start_clears_the_harness_its_earlier_launch_failed_on(rig):
    admitted = rig.admitter.admit(SLUG, ask())
    rig.store.note_launch_failure(SLUG, "t1", "launch failed", "codex")

    claims.Claims(rig.store).handle(started(admitted))

    assert rig.store.failed_harness(SLUG, "t1") == ""


@pytest.mark.feature("events-started-working")
def test_an_admitted_then_started_agent_is_working_with_its_pane_and_launch_time(rig):
    admitted = rig.admitter.admit(SLUG, ask())
    event = started(admitted)

    claims.Claims(rig.store).handle(replace(event, data={**event.data, "launched_at": 6_500}))

    [agent] = rig.store.agents(SLUG)
    assert (agent.name, agent.state, agent.pane_id, agent.launched_at) == (admitted["name"], "working", "w1:p3", 6_500)


@pytest.mark.feature("events-started-working")
def test_a_start_from_a_spoke_without_a_launch_time_takes_the_event_time(rig):
    claims.Claims(rig.store).handle(replace(started(rig.admitter.admit(SLUG, ask())), time=7_100))

    [agent] = rig.store.agents(SLUG)
    assert (agent.state, agent.pane_id, agent.launched_at) == ("working", "w1:p3", 7_100)


@pytest.mark.feature("events-started-working")
def test_a_late_start_never_revives_a_finished_agent(rig):
    event = started(rig.admitter.admit(SLUG, ask()))
    [agent] = rig.store.agents(SLUG)
    rig.store.execution_registry.update(SLUG, replace(agent, state="finished"))

    claims.Claims(rig.store).handle(replace(event, data={**event.data, "launched_at": 6_500}))

    [agent] = rig.store.agents(SLUG)
    assert (agent.state, agent.pane_id, agent.launched_at) == ("finished", "", 0)


@pytest.mark.feature("events-started-working")
def test_a_launch_decision_after_the_start_still_reaches_the_working_agent(rig):
    from tests.hub.test_admission import LAUNCH

    claims.Claims(rig.store).handle(started(rig.admitter.admit(SLUG, ask())))

    assert rig.admitter.launched(SLUG, ask(launch=LAUNCH)) is True
    [agent] = rig.store.agents(SLUG)
    assert (agent.state, agent.pane_id, agent.harness, agent.model) == ("working", "w1:p3", "codex", "gpt-5")


@pytest.mark.feature("events-started-working")
@pytest.mark.parametrize("launched_at", [0, None])
def test_a_start_with_an_empty_launch_time_takes_the_event_time(rig, launched_at):
    event = replace(started(rig.admitter.admit(SLUG, ask())), time=7_100)

    claims.Claims(rig.store).handle(replace(event, data={**event.data, "launched_at": launched_at}))

    [agent] = rig.store.agents(SLUG)
    assert (agent.state, agent.launched_at) == ("working", 7_100)


@pytest.mark.feature("events-started-working")
def test_a_rebuilt_start_without_a_pane_counts_the_life_and_leaves_the_agent_starting(rig):
    event = started(rig.admitter.admit(SLUG, ask()))

    claims.Claims(rig.store).handle(replace(event, data={**event.data, "pane_id": ""}))

    [agent] = rig.store.agents(SLUG)
    assert (agent.state, agent.pane_id, agent.launched_at) == ("starting", "", 0)
    assert rig.store.claims(SLUG, "t1") == 1


@pytest.mark.feature("events-started-working")
def test_a_working_write_that_raises_counts_no_life_so_redelivery_retries_both(rig, monkeypatch):
    event = started(rig.admitter.admit(SLUG, ask()))
    registry = rig.store.execution_registry
    real = registry.set_launch

    def refused(*args, **kwargs):
        raise ConnectionError("redis gone")

    monkeypatch.setattr(registry, "set_launch", refused)
    with pytest.raises(ConnectionError):
        claims.Claims(rig.store).handle(event)
    assert rig.store.claims(SLUG, "t1") == 0

    monkeypatch.setattr(registry, "set_launch", real)
    claims.Claims(rig.store).handle(replace(event, id="redelivery"))

    [agent] = rig.store.agents(SLUG)
    assert (agent.state, agent.pane_id, rig.store.claims(SLUG, "t1")) == ("working", "w1:p3", 1)


@pytest.mark.feature("events-started-working")
def test_a_projection_that_raises_after_the_write_is_projected_again_on_redelivery(rig, monkeypatch):
    event = started(rig.admitter.admit(SLUG, ask()))
    project = rig.store.project_agent

    def refused(*args, **kwargs):
        raise ConnectionError("redis gone")

    monkeypatch.setattr(rig.store, "project_agent", refused)
    with pytest.raises(ConnectionError):
        claims.Claims(rig.store).handle(event)
    monkeypatch.setattr(rig.store, "project_agent", project)
    [shown] = rig.store.agents(SLUG)
    assert (shown.state, rig.store.claims(SLUG, "t1")) == ("starting", 0)

    claims.Claims(rig.store).handle(replace(event, id="redelivery"))

    [agent] = rig.store.agents(SLUG)
    assert (agent.state, agent.pane_id, rig.store.claims(SLUG, "t1")) == ("working", "w1:p3", 1)


@pytest.mark.parametrize("state, lives", [("started", 1), ("failed", 1)])
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


def test_a_refund_racing_a_new_pending_start_cancels_that_start(rig, monkeypatch):
    for _ in range(2):
        rig.store.count_claim(SLUG, "t1")
    assert rig.store.claim(SLUG, "t1", "engineer@race", 60_000)
    real, pending = rig.store._started_lives, rig.store.key(SLUG, "pending-lives")

    def racing(slug, task):
        monkeypatch.setattr(rig.store, "_started_lives", real)
        rig.store.redis.hset(pending, task, '["execution", 1]')
        return real(slug, task)

    monkeypatch.setattr(rig.store, "_started_lives", racing)

    assert rig.store.refund_held_claim(SLUG, "t1", "engineer@race", rig.store.claim_generation(SLUG, "t1"))
    assert rig.store.claims(SLUG, "t1") == 2
    assert rig.store.redis.hget(pending, "t1") is None


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


def test_a_legacy_counted_start_drops_a_stale_pending_life_so_its_refund_spends_a_counted_life(rig):
    rig.admitter.admit(SLUG, ask())
    rig.store.release(SLUG, "t1", rig.store.claimant(SLUG, "t1"))
    assert rig.store.claim(SLUG, "t1", "engineer@legacy", 60_000)
    rig.store.count_claim(SLUG, "t1")

    assert rig.store.refund_held_claim(SLUG, "t1", "engineer@legacy", rig.store.claim_generation(SLUG, "t1"))
    assert rig.store.claims(SLUG, "t1") == 0


@pytest.mark.feature("hub-conversation-overlays")
def test_a_started_agent_records_the_conversation_and_overlays_its_hive_placed(rig):
    event = started(rig.admitter.admit(SLUG, ask()))
    [admitted] = rig.store.agents(SLUG)
    assert (admitted.conversation_id, admitted.overlays) == ("", [])

    claims.Claims(rig.store).handle(
        replace(event, data={**event.data, "conversation_id": "thread-a", "overlays": ["tuner", "brain"]})
    )

    [agent] = rig.store.agents(SLUG)
    assert (agent.state, agent.conversation_id, agent.overlays) == ("working", "thread-a", ["tuner", "brain"])


@pytest.mark.feature("hub-conversation-overlays")
def test_a_start_naming_no_conversation_or_overlays_keeps_the_recorded_ones(rig):
    event = started(rig.admitter.admit(SLUG, ask()))
    [agent] = rig.store.agents(SLUG)
    rig.store.execution_registry.update(SLUG, replace(agent, conversation_id="thread-b", overlays=["brain"]))

    claims.Claims(rig.store).handle(replace(event, data={**event.data, "conversation_id": "", "overlays": []}))

    [agent] = rig.store.agents(SLUG)
    assert (agent.state, agent.conversation_id, agent.overlays) == ("working", "thread-b", ["brain"])
