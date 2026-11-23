from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from scripts.hive import admission
from scripts.hive.orders import Ack
from scripts.swarm import expert_lineage, expert_seat, lease, seat_spawn, tick
from scripts.swarm.controller import FencedRuntime
from scripts.swarm.profile_choice import ProfileDecision
from scripts.swarm_v2.runtime.base import RuntimeRouter
from scripts.swarm_v2.runtime.routed import RoutedRuntime
from tests.hive.test_controller_launch import setup as hive_setup
from tests.swarm.test_expert_mission import GOOD
from tests.swarm.test_tick import FakeRuntime

pytestmark = pytest.mark.unit
_fixtures = (hive_setup,)


@pytest.fixture
def context(hive_setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    ctx = hive_setup
    ctx.herdr._profile_decision.return_value = ProfileDecision("expert", "task", "expert")
    ctx.herdr.has_capacity.return_value = True
    ctx.runtime = FencedRuntime(
        ctx.store, ctx.config.slug, ctx.held, RoutedRuntime(ctx.herdr, RuntimeRouter([])), "distributed"
    )
    ctx.launcher = ctx.runtime.hive_launch
    ctx.launcher.opening = lambda request: f"opening for {request.name}"
    ctx.launcher.sleep = Mock(side_effect=AssertionError("an expert launch must not wait for its hive"))
    monkeypatch.setattr(seat_spawn, "host_hold", lambda slug, store, now_ms, what: "")
    ctx.lineage = expert_lineage.start(
        ctx.store, ctx.config.slug, GOOD, "/missions/expert.md", "xhigh", "master", lease.now_ms(ctx.store)
    )
    return ctx


def run(ctx: SimpleNamespace, runtime: Any = None) -> list[str]:
    return expert_seat.run(
        ctx.config.slug, ctx.config, ctx.store, ctx.runtime if runtime is None else runtime, lease.now_ms(ctx.store)
    )


def pending(ctx: SimpleNamespace) -> Any:
    run(ctx)
    ctx.launcher.sleep.assert_not_called()
    [agent] = [agent for agent in ctx.store.agents(ctx.config.slug) if agent.lane == "expert"]
    assert agent.state == "launching"
    order = ctx.launcher.orders_for(ctx.lineage.id)
    assert order is not None and order.execution_id == agent.execution_id
    assert ctx.store.seats.occupant(ctx.lineage.seat).occupant == agent.name
    assert expert_lineage.launches(ctx.store, ctx.config.slug, ctx.lineage.id) == 1
    return agent


def acknowledge(ctx: SimpleNamespace, ack: Ack) -> None:
    order = ctx.launcher.orders_for(ctx.lineage.id)
    assert order is not None
    ctx.store.redis.hset(ctx.launcher.orders.receipt(order), "ack", ack.encode())


@pytest.mark.feature("expert-seat-step")
def test_existing_expert_mission_and_working_record_keep_their_identity(context: SimpleNamespace) -> None:
    runtime = FakeRuntime()
    run(context, runtime)
    [agent] = [agent for agent in context.store.agents(context.config.slug) if agent.lane == "expert"]
    [task] = runtime.tasks
    assert (task["mission"], task["generation"], task["expert_effort"], task["seat"]) == (
        context.lineage.mission,
        1,
        "xhigh",
        context.lineage.seat,
    )
    assert agent.state == "working"
    assert run(context, runtime) == []
    assert len(runtime.spawned) == 1
    assert expert_lineage.launches(context.store, context.config.slug, context.lineage.id) == 1


@pytest.mark.feature("expert-seat-step")
def test_expert_hive_launch_returns_pending_without_waiting_or_releasing_its_reservation(
    context: SimpleNamespace,
) -> None:
    agent = pending(context)
    assert admission.pending(context.store.redis, agent.hive) == 1
    order = context.launcher.orders_for(context.lineage.id)
    assert order is not None
    assert context.launcher.slots.counts() == {"claude:account": 1}


@pytest.mark.feature("expert-seat-step")
def test_next_expert_pass_keeps_the_same_unacknowledged_order_and_generation(context: SimpleNamespace) -> None:
    agent = pending(context)
    order = context.launcher.orders_for(context.lineage.id)
    run(context)
    [kept] = [agent for agent in context.store.agents(context.config.slug) if agent.lane == "expert"]
    assert kept == agent
    assert context.launcher.orders_for(context.lineage.id) == order
    assert len(context.store.execution_registry.records(context.config.slug)) == 1
    assert expert_lineage.launches(context.store, context.config.slug, context.lineage.id) == 1
    context.launcher.sleep.assert_not_called()


@pytest.mark.feature("expert-seat-step")
def test_next_expert_pass_records_the_live_acknowledgement_on_the_original_execution(context: SimpleNamespace) -> None:
    agent = pending(context)
    acknowledge(
        context,
        Ack(
            "live",
            {"host_id": "remote", "namespace": "expert"},
            execution={
                "pane_id": "expert-pane",
                "model": "fable",
                "effort": "xhigh",
                "profile_decision": {"validation": {"pid": 31}},
            },
        ),
    )
    run(context)
    [live] = [agent for agent in context.store.agents(context.config.slug) if agent.lane == "expert"]
    assert (live.name, live.seat, live.execution_id, live.generation) == (
        agent.name,
        agent.seat,
        agent.execution_id,
        agent.generation,
    )
    assert (live.state, live.pane_id, live.model, live.effort, live.account) == (
        "working",
        "expert-pane",
        "fable",
        "xhigh",
        "account",
    )
    assert live.profile_decision["validation"] == {"pid": 31}
    assert expert_lineage.launches(context.store, context.config.slug, context.lineage.id) == 1
    assert admission.pending(context.store.redis, agent.hive) == 0
    assert run(context) == []
    context.launcher.sleep.assert_not_called()


@pytest.mark.feature("expert-seat-step")
def test_next_expert_pass_records_a_refusal_and_keeps_the_lineage_available_for_retry(context: SimpleNamespace) -> None:
    agent = pending(context)
    acknowledge(context, Ack("failed", {}, failure="profile refused"))
    actions = run(context)
    assert any("profile refused" in action for action in actions)
    assert not [agent for agent in context.store.agents(context.config.slug) if agent.lane == "expert"]
    assert context.store.execution_registry.get(context.config.slug, agent.execution_id).state == "ended"
    assert admission.pending(context.store.redis, agent.hive) == 0
    assert expert_lineage.running(context.store, context.config.slug) == [context.lineage]


@pytest.mark.feature("expert-seat-step")
def test_expired_expert_order_is_retired_without_launching_again_under_the_same_name(
    context: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = pending(context)
    order = context.launcher.orders_for(context.lineage.id)
    assert order is not None
    monkeypatch.setattr(lease, "now_ms", lambda store: order.expires_at)
    run(context)
    assert context.store.execution_registry.get(context.config.slug, agent.execution_id).state == "ended"
    assert len(context.store.execution_registry.records(context.config.slug)) == 1
    assert not [agent for agent in context.store.agents(context.config.slug) if agent.lane == "expert"]
    context.launcher.sleep.assert_not_called()


@pytest.mark.feature("expert-seat-step")
def test_reap_keeps_a_pending_expert_until_its_order_is_collected(context: SimpleNamespace) -> None:
    agent = pending(context)
    assert tick._acknowledging(context.config.slug, context.store, agent)


@pytest.mark.feature("expert-seat-step")
@pytest.mark.parametrize("status", ["pending", "refused"])
def test_fenced_spawn_preserves_reservations_only_while_acknowledgement_is_pending(
    context: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    error = tick.SpawnError("hive result", status)
    monkeypatch.setattr(context.runtime, "_spawn", Mock(side_effect=error))
    released = Mock()
    monkeypatch.setattr(admission, "release", released)
    with pytest.raises(tick.SpawnError, match="hive result"):
        context.runtime.spawn(context.config, "expert", "expert-agent", {"id": context.lineage.id})
    assert released.call_count == (0 if status == "pending" else 1)
