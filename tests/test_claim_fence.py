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


@pytest.mark.parametrize("state", [None, "ready", "withdrawn", "dead"])
def test_revoked_or_missing_claim_ends_the_old_agent(monkeypatch, state):
    from hooks.context import claim_fence

    bus = open_bus(BusConfig())
    if state is not None:
        bus.state("spoke-work").create("claim-a", json.dumps({"state": state}).encode())
    monkeypatch.setenv("AGENTIHOOKS_CLAIM_FENCE", json.dumps({"key": "claim-a", "revision": 1}))
    with (
        patch.object(claim_fence, "open_bus", return_value=bus),
        patch.object(claim_fence, "agent_pid", return_value=4321),
        patch.object(claim_fence, "terminate") as kill,
    ):
        assert claim_fence.check({"hook_event_name": "PreToolUse"}) == claim_fence.REVOKED
    kill.assert_called_once_with(4321)


def test_unreachable_authority_blocks_actions_without_killing_the_agent(monkeypatch):
    from hooks.context import claim_fence

    monkeypatch.setenv("AGENTIHOOKS_CLAIM_FENCE", json.dumps({"key": "claim-a", "revision": 1}))
    with (
        patch.object(claim_fence, "open_bus", side_effect=RuntimeError("offline")),
        patch.object(claim_fence, "terminate") as kill,
    ):
        assert claim_fence.check({"hook_event_name": "PreToolUse"}) == claim_fence.CHECK_FAILED
    kill.assert_not_called()


@pytest.mark.parametrize("message", [None, "claim revoked"])
def test_shared_dispatch_fence_denies_an_action_before_its_handler(message):
    from hooks.context import claim_fence
    from hooks.hook_manager import BlockAction

    payload = {"hook_event_name": "PreToolUse"}
    with patch.object(claim_fence, "check", return_value=message) as check:
        if message:
            with pytest.raises(BlockAction, match=message):
                claim_fence.enforce(payload)
        else:
            assert claim_fence.enforce(payload) is None
    check.assert_called_once_with(payload)


@pytest.mark.parametrize("pid", [0, 1, 4321, 9876])
def test_fence_signals_only_a_valid_owning_agent_process(pid):
    import signal

    from hooks.context import claim_fence

    with patch.object(claim_fence.os, "getpid", return_value=9876), patch.object(claim_fence.os, "kill") as kill:
        claim_fence.terminate(pid)
    assert kill.call_args_list == ([((4321, signal.SIGTERM), {})] if pid == 4321 else [])


def test_fence_can_end_its_actual_parent_agent_process():
    import signal
    import subprocess
    import sys

    child = "from hooks.context.claim_fence import terminate; import os; terminate(os.getppid())"
    parent = f"import subprocess, sys; subprocess.run([sys.executable, '-c', {child!r}], check=True)"
    outcome = subprocess.run([sys.executable, "-c", parent], capture_output=True, text=True, timeout=15)
    assert outcome.returncode == -signal.SIGTERM, outcome.stderr


def test_an_agent_that_already_exited_needs_no_second_signal():
    from hooks.context import claim_fence

    with (
        patch.object(claim_fence.os, "getpid", return_value=9876),
        patch.object(claim_fence.os, "kill", side_effect=ProcessLookupError) as kill,
    ):
        claim_fence.terminate(4321)
    kill.assert_called_once()


def test_session_end_does_not_reenter_claim_termination(monkeypatch):
    from hooks.context import claim_fence

    monkeypatch.setenv("AGENTIHOOKS_CLAIM_FENCE", "revoked")
    with patch.object(claim_fence, "open_bus", side_effect=AssertionError("already ending")):
        assert claim_fence.check({"hook_event_name": "SessionEnd"}) is None


def test_an_initial_launching_claim_is_owned_at_its_pinned_revision(monkeypatch):
    from hooks.context import claim_fence

    bus = open_bus(BusConfig())
    revision = bus.state("spoke-work").create("claim-a", b'{"state":"launching"}')
    monkeypatch.setenv("AGENTIHOOKS_CLAIM_FENCE", json.dumps({"key": "claim-a", "revision": revision}))
    with patch.object(claim_fence, "open_bus", return_value=bus), patch.object(claim_fence, "terminate") as kill:
        assert claim_fence.check({"hook_event_name": "SessionStart"}) is None
    kill.assert_not_called()
