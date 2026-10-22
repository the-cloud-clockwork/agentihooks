import json
from unittest.mock import Mock, patch

import pytest

from scripts.events.config import BusConfig
from scripts.events.memory import open_bus


@pytest.mark.parametrize("changed", [False, True])
def test_agent_hook_terminates_only_the_process_with_a_superseded_claim(monkeypatch, changed):
    from hooks.context import claim_fence

    bus = open_bus(BusConfig())
    claims = bus.state("spoke-work")
    revision = claims.create("claim-a", b'{"state":"launching"}')
    claims.update("claim-a", json.dumps({"state": "started", "claim_revision": revision}).encode(), revision)
    if changed:
        current = claims.get("claim-a")
        claims.update("claim-a", b'{"state":"claimed"}', current.revision)
    monkeypatch.setenv("AGENTIHOOKS_CLAIM_FENCE", json.dumps({"key": "claim-a", "revision": revision}))
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "rig")
    kill = Mock()
    try:
        with (
            patch.object(claim_fence, "open_bus", return_value=bus),
            patch.object(claim_fence, "agent_pid", return_value=4321),
            patch.object(claim_fence, "terminate", kill),
        ):
            message = claim_fence.check({"hook_event_name": "PreToolUse", "session_id": "agent-session"})
        assert bool(message) is changed
        assert kill.call_args_list == ([((4321,), {})] if changed else [])
    finally:
        bus.close()


def test_unclaimed_agent_does_not_connect_to_the_bus(monkeypatch):
    from hooks.context import claim_fence

    monkeypatch.delenv("AGENTIHOOKS_CLAIM_FENCE", raising=False)
    with patch.object(claim_fence, "open_bus", side_effect=AssertionError("unclaimed")):
        assert claim_fence.check({"hook_event_name": "PreToolUse"}) is None


def test_launcher_preserves_the_claim_fence_for_every_harness():
    from scripts.init_agent import _swarm_exports

    fence = json.dumps({"key": "claim-a", "revision": 7})
    exports = _swarm_exports({"AGENTIHOOKS_SWARM": "rig", "AGENTIHOOKS_CLAIM_FENCE": fence})
    assert "export AGENTIHOOKS_CLAIM_FENCE=" in exports
    assert fence in exports
