import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

from scripts.events.config import BusConfig, QueuePolicy
from scripts.events.memory import open_bus
from scripts.hive import lanes
from scripts.hive.spoke import Spoke, claim_key
from scripts.hub import slots
from scripts.swarm import capacity, readiness, runtime
from scripts.swarm.store import SwarmConfig
from scripts.swarm_v2.runtime.base import Outcome, Status
from tests.hive.test_lanes import Hub, offer


@pytest.mark.feature("event-offer-account-seats")
@pytest.mark.parametrize(("requires_harness", "codex_sessions"), [("claude", 0), ("claude", 6), ("", 6)])
def test_full_account_offers_keep_their_owner_generation_and_delivery_until_a_seat_returns(
    monkeypatch: pytest.MonkeyPatch, requires_harness: str, codex_sessions: int
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
    config = SwarmConfig("rig", "/repo", 1, 1, scaling="manual", lanes={"eng": {"agent": "auto"}})
    store = SimpleNamespace(slugs=lambda: ["rig"], config=lambda _slug: config)
    local = Mock()
    harness = requires_harness or "codex"
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

        selected = 0 if requires_harness else 1
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
