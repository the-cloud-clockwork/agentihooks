import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

from scripts.events.config import BusConfig, QueuePolicy
from scripts.events.memory import open_bus
from scripts.hive import lanes
from scripts.hive.spoke import Spoke, SpokeConfig, claim_key
from scripts.hub import slots
from scripts.swarm import capacity, offer_quota, readiness, runtime
from scripts.swarm.offer_quota import accounts as real_accounts
from scripts.swarm.store import SwarmConfig
from scripts.swarm_v2.runtime.base import Outcome, Status
from tests.hive.test_lanes import Hub, offer


@pytest.fixture(autouse=True)
def _seats_read_through_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(offer_quota, "accounts", real_accounts)


@pytest.mark.feature("event-offer-account-seats")
@pytest.mark.parametrize(
    ("requires_harness", "codex_sessions", "lane_agent"),
    [("claude", 0, "auto"), ("claude", 6, "auto"), ("", 6, "auto"), ("", 0, "claude")],
)
def test_full_account_offers_keep_their_owner_generation_and_delivery_until_a_seat_returns(
    monkeypatch: pytest.MonkeyPatch, requires_harness: str, codex_sessions: int, lane_agent: str
) -> None:
    rows = [
        capacity.Account("claude", "claude-a", "OPEN", 3, None, 90, 3),
        capacity.Account("codex", "codex-a", "OPEN", codex_sessions, None, 90, 6),
    ]
    monkeypatch.setattr(capacity, "accounts", Mock(side_effect=lambda *_args, **_kwargs: list(rows)))
    monkeypatch.setattr(runtime.plugins, "claude_only", lambda _profile: False)
    monkeypatch.setattr(Spoke, "_measure", lambda _self: {"cpus": 32, "load5": 0, "available_mb": 60_000})
    clock = SimpleNamespace(now=1_000)
    bus = open_bus(BusConfig(scoped=True), clock=lambda: clock.now)
    profile = "planner" if requires_harness else "engineer"
    event = offer()
    event = replace(event, data={**event.data, "quota": {"profile": profile, "requires_harness": requires_harness}})
    original = event.to_dict()
    admitted = {
        "hive": "hive-a",
        "name": "eng@rig-0001",
        "task": {
            "id": event.key,
            "seat": "eng-1@rig",
            "execution_id": "event-execution",
            "execution_generation": 7,
            "claim_generation": event.version,
            "profile": profile,
            "requires_harness": requires_harness,
        },
    }
    hub = Hub(admitted)
    slots.serve(cast(Any, bus), admitter=cast(Any, hub))
    config = SwarmConfig("rig", "/repo", 1, 1, scaling="manual", lanes={"eng": {"agent": lane_agent}})
    store = SimpleNamespace(slugs=lambda: ["rig"], config=lambda _slug: config)
    local = Mock()
    harness = requires_harness or ("codex" if lane_agent == "auto" else lane_agent)
    local.spawn.return_value = Outcome(
        "spawn",
        Status.OK,
        "local",
        SimpleNamespace(pane_id="pane-a", harness=harness, account=f"{harness}-a", profile=profile, launched_at=1_500),
    )
    spoke = lanes.spokes(store, bus, local, "hive-a", ("eng",), {})[0]
    queue = bus.for_swarm("rig").queue(readiness.queue_name("rig", "eng"), QueuePolicy())
    queue.offer(event)
    claims = spoke.bus.state("spoke-work")
    claim = json.dumps({"owner": "earlier-hive", "state": "ready"}).encode()
    revision = claims.create(claim_key(event), claim)
    reserve = Mock(wraps=spoke._reserve)
    monkeypatch.setattr(spoke, "_reserve", reserve)

    try:
        for _ in range(spoke.config.policy.max_deliver + 2):
            assert spoke.poll() == "held"
            kept = claims.get(claim_key(event))
            assert kept is not None and (kept.value, kept.revision) == (claim, revision)
            assert spoke.bus.state("spoke-pending").get(spoke._pending_key) is None
            assert hub.seen == []
            local.spawn.assert_not_called()
            reserve.assert_not_called()
            assert event.to_dict() == original
            clock.now += spoke.config.policy.ack_wait_ms + readiness.UNFIT_NAK_MS

        selected = 1 if harness == "codex" else 0
        rows[selected] = capacity.Account(harness, f"{harness}-a", "OPEN", 0, None, 90, 6)

        assert spoke.poll() == "started"
        local.spawn.assert_called_once()
        reserve.assert_called_once()
        delivery = reserve.call_args.args[0]
        assert delivery.attempt == 1
        assert delivery.event.to_dict() == original
        assert [entry[0] for entry in hub.seen] == ["admit", "launched"]
        assert hub.seen[0][2]["generation"] == event.version
        spawned = local.spawn.call_args.args[0]
        assert spawned.task["profile"] == profile
        assert spawned.task["requires_harness"] == requires_harness
        assert spawned.task["execution_generation"] == 7
        assert spawned.task["claim_generation"] == event.version
    finally:
        bus.close()


@pytest.mark.feature("compose-smoke-spoke-seat")
def test_a_spoke_reads_seats_through_its_own_account_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    production = Mock(return_value=[])
    monkeypatch.setattr(capacity, "accounts", production)
    monkeypatch.setattr(runtime.plugins, "claude_only", lambda _profile: False)
    monkeypatch.setattr(Spoke, "_measure", lambda _self: {"cpus": 32, "load5": 0, "available_mb": 60_000})
    clock = SimpleNamespace(now=1_000)
    bus = open_bus(BusConfig(scoped=True), clock=lambda: clock.now)
    event = offer()
    admitted = {
        "hive": "hive-a",
        "name": "eng@rig-0001",
        "task": {
            "id": event.key,
            "seat": "eng-1@rig",
            "execution_id": "event-execution",
            "execution_generation": 7,
            "claim_generation": event.version,
            "profile": "engineer",
            "requires_harness": "",
        },
    }
    slots.serve(cast(Any, bus), admitter=cast(Any, Hub(admitted)))
    config = SwarmConfig("rig", "/repo", 1, 1, scaling="manual")
    local = Mock()
    local.spawn.return_value = Outcome(
        "spawn",
        Status.OK,
        "local",
        SimpleNamespace(pane_id="pane-a", harness="claude", account="claude-a", profile="engineer", launched_at=1_500),
    )
    seats: list[capacity.Account] = []
    spoke = Spoke(
        SpokeConfig(config, "eng", "hive-a", accounts=lambda *_args, **_kwargs: list(seats)),
        cast(Any, bus),
        local,
        lanes.LaneTasks(bus, "hive-a", lambda _slug: config),
        view=cast(Any, lambda: None),
    )
    bus.for_swarm("rig").queue(readiness.queue_name("rig", "eng"), QueuePolicy()).offer(event)
    try:
        assert spoke.poll() == "held"
        clock.now += spoke.config.policy.ack_wait_ms + readiness.UNFIT_NAK_MS
        seats.append(capacity.Account("claude", "claude-a", "OPEN", 0, None, 90, 6))
        assert spoke.poll() == "started"
        local.spawn.assert_called_once()
        production.assert_not_called()
    finally:
        bus.close()
