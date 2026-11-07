import hashlib
import json
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from scripts.events.config import BusConfig
from scripts.events.memory import MemoryBus, open_bus


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
            patch.object(claim_fence, "open_bus", return_value=bus) as connect,
            patch.object(claim_fence, "agent_pid", return_value=4321),
            patch.object(claim_fence, "terminate", kill),
        ):
            message = claim_fence.check({"hook_event_name": "PreToolUse", "session_id": "agent-session"})
        connect.assert_called_once_with(BusConfig.from_environ())
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
    exports = _swarm_exports(
        {
            "AGENTIHOOKS_SWARM": "rig",
            "AGENTIHOOKS_CLAIM_FENCE": fence,
            "AGENTIHOOKS_BUS": "nats",
            "AGENTIHOOKS_BUS_URL": "nats://hive.invalid:4222",
        }
    )
    assert "export AGENTIHOOKS_BUS=nats\n" in exports
    assert "export AGENTIHOOKS_BUS_URL=nats://hive.invalid:4222\n" in exports
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


def test_unreachable_authority_lets_actions_pass_without_killing_the_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    from hooks.context import claim_fence

    monkeypatch.setenv("AGENTIHOOKS_CLAIM_FENCE", json.dumps({"key": "claim-a", "revision": 1}))
    with (
        patch.object(claim_fence, "open_bus", side_effect=RuntimeError("offline")) as connect,
        patch.object(claim_fence, "sleep") as pause,
        patch.object(claim_fence, "terminate") as kill,
    ):
        assert claim_fence.check({"hook_event_name": "PreToolUse"}) is None
    assert claim_fence.ATTEMPTS == 3
    assert connect.call_count == 3
    assert pause.call_args_list == [((claim_fence.DELAY,), {}), ((claim_fence.DELAY,), {})]
    kill.assert_not_called()


def test_a_launch_prompt_passes_when_one_bus_read_fails_before_an_owned_claim(monkeypatch):
    from hooks.context import claim_fence

    bus = open_bus(BusConfig())
    revision = bus.state("spoke-work").create("claim-a", b'{"state":"launching"}')
    monkeypatch.setenv("AGENTIHOOKS_CLAIM_FENCE", json.dumps({"key": "claim-a", "revision": revision}))
    with (
        patch.object(claim_fence, "open_bus", side_effect=[RuntimeError("timeout"), bus]) as connect,
        patch.object(claim_fence, "sleep") as pause,
        patch.object(claim_fence, "terminate") as kill,
    ):
        assert claim_fence.check({"hook_event_name": "UserPromptSubmit"}) is None
    assert connect.call_count == 2
    pause.assert_called_once_with(claim_fence.DELAY)
    kill.assert_not_called()


def test_a_superseded_claim_is_still_refused_after_a_transient_read_failure(monkeypatch):
    from hooks.context import claim_fence

    bus = open_bus(BusConfig())
    bus.state("spoke-work").create("claim-a", b'{"state":"started","claim_revision":9}')
    monkeypatch.setenv("AGENTIHOOKS_CLAIM_FENCE", json.dumps({"key": "claim-a", "revision": 1}))
    with (
        patch.object(claim_fence, "open_bus", side_effect=[RuntimeError("timeout"), bus]) as connect,
        patch.object(claim_fence, "sleep") as pause,
        patch.object(claim_fence, "agent_pid", return_value=4321),
        patch.object(claim_fence, "terminate") as kill,
    ):
        assert claim_fence.check({"hook_event_name": "UserPromptSubmit"}) == claim_fence.REVOKED
    assert connect.call_count == 2
    pause.assert_called_once_with(claim_fence.DELAY)
    kill.assert_called_once_with(4321)


@pytest.mark.parametrize("recovers", [True, False])
def test_a_failed_claim_state_read_is_retried_before_passing(monkeypatch: pytest.MonkeyPatch, recovers: bool) -> None:
    from hooks.context import claim_fence

    bus = Mock()
    owned = Mock(value=b'{"state":"launching"}', revision=5)
    failure = RuntimeError("read timeout")
    bus.state.return_value.get.side_effect = [failure, owned] if recovers else [failure, failure, failure]
    monkeypatch.setenv("AGENTIHOOKS_CLAIM_FENCE", json.dumps({"key": "claim-a", "revision": 5}))
    with (
        patch.object(claim_fence, "open_bus", return_value=bus),
        patch.object(claim_fence, "sleep") as pause,
        patch.object(claim_fence, "terminate") as kill,
    ):
        message = claim_fence.check({"hook_event_name": "UserPromptSubmit"})
    attempts = 2 if recovers else 3
    assert message is None
    assert bus.state.call_args_list == [(("spoke-work",), {})] * attempts
    assert bus.state.return_value.get.call_args_list == [(("claim-a",), {})] * attempts
    assert bus.close.call_count == attempts
    assert pause.call_args_list == [((claim_fence.DELAY,), {})] * (attempts - 1)
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


@pytest.mark.parametrize("pid", [0, 1, 2, 4321, 9876])
def test_fence_signals_only_a_valid_owning_agent_process(pid):
    import signal

    from hooks.context import claim_fence

    with patch.object(claim_fence.os, "getpid", return_value=9876), patch.object(claim_fence.os, "kill") as kill:
        claim_fence.terminate(pid)
    assert kill.call_args_list == ([((pid, signal.SIGTERM), {})] if pid in (2, 4321) else [])


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


def test_session_end_does_not_reenter_claim_termination(monkeypatch: pytest.MonkeyPatch) -> None:
    from hooks.context import claim_fence

    monkeypatch.setenv("AGENTIHOOKS_CLAIM_FENCE", "revoked")
    with (
        patch.object(claim_fence, "fresh", return_value=False),
        patch.object(claim_fence, "bounded", return_value=False) as read,
        patch.object(claim_fence, "terminate") as kill,
    ):
        assert claim_fence.check({"hook_event_name": "SessionEnd"}) is None
    read.assert_not_called()
    kill.assert_not_called()


def test_an_initial_launching_claim_is_owned_at_its_pinned_revision(monkeypatch):
    from hooks.context import claim_fence

    bus = open_bus(BusConfig())
    revision = bus.state("spoke-work").create("claim-a", b'{"state":"launching"}')
    monkeypatch.setenv("AGENTIHOOKS_CLAIM_FENCE", json.dumps({"key": "claim-a", "revision": revision}))
    with patch.object(claim_fence, "open_bus", return_value=bus), patch.object(claim_fence, "terminate") as kill:
        assert claim_fence.check({"hook_event_name": "SessionStart"}) is None
    kill.assert_not_called()


def test_hook_dispatch_passes_the_normalized_payload_through_the_claim_fence():
    import io

    from hooks import hook_manager
    from hooks.context import claim_fence

    payload = {"hook_event_name": "Notification", "session_id": "claim-session"}
    handler = Mock()
    with (
        patch.object(hook_manager.sys, "stdin", io.StringIO(json.dumps(payload))),
        patch.object(hook_manager.os, "_exit", side_effect=hook_manager.sys.exit),
        patch.dict(hook_manager.EVENT_HANDLERS, {"Notification": handler}),
        patch.object(claim_fence, "check", return_value=None) as check,
    ):
        with pytest.raises(SystemExit) as exited:
            hook_manager.main()
    assert exited.value.code == 0
    check.assert_called_once_with(payload)
    handler.assert_called_once_with(payload)


def _held_claim(monkeypatch: pytest.MonkeyPatch) -> MemoryBus:
    bus = open_bus(BusConfig())
    revision = bus.state("spoke-work").create("claim-a", b'{"state":"started"}')
    monkeypatch.setenv("AGENTIHOOKS_CLAIM_FENCE", json.dumps({"key": "claim-a", "revision": revision}))
    monkeypatch.setattr(bus, "close", lambda: None)
    return bus


def _stalled_bus(release: threading.Event) -> Callable[[BusConfig], None]:
    def stall(_config: BusConfig) -> None:
        release.wait(10)
        raise RuntimeError("stalled")

    return stall


@pytest.mark.wall_clock
def test_slow_nats_costs_at_most_the_bound_and_lets_the_action_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    from hooks.context import claim_fence

    _held_claim(monkeypatch)
    release = threading.Event()
    try:
        with (
            patch.object(claim_fence, "open_bus", side_effect=_stalled_bus(release)),
            patch.object(claim_fence, "terminate") as kill,
        ):
            started = time.monotonic()
            message = claim_fence.check({"hook_event_name": "PreToolUse"})
            elapsed = time.monotonic() - started
    finally:
        release.set()
    assert claim_fence.BOUND == 1.0
    assert elapsed < claim_fence.BOUND + 0.5
    assert message is None
    kill.assert_not_called()


def test_a_moved_claim_still_blocks_once_the_cached_answer_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    from hooks.context import claim_fence

    bus = _held_claim(monkeypatch)
    with (
        patch.object(claim_fence, "open_bus", return_value=bus),
        patch.object(claim_fence, "agent_pid", return_value=4321),
        patch.object(claim_fence, "terminate") as kill,
    ):
        assert claim_fence.check({"hook_event_name": "PreToolUse"}) is None
        current = bus.state("spoke-work").get("claim-a")
        assert current is not None
        bus.state("spoke-work").update("claim-a", b'{"state":"claimed"}', current.revision)
        with patch.object(claim_fence, "CACHE_SECONDS", 0):
            assert claim_fence.check({"hook_event_name": "PreToolUse"}) == claim_fence.REVOKED
    kill.assert_called_once_with(4321)


def test_a_fresh_held_answer_skips_the_read_for_its_own_claim_generation_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hooks.context import claim_fence

    bus = _held_claim(monkeypatch)
    with patch.object(claim_fence, "open_bus", return_value=bus):
        assert claim_fence.check({"hook_event_name": "PreToolUse"}) is None
    with patch.object(claim_fence, "open_bus") as cached:
        assert claim_fence.check({"hook_event_name": "PreToolUse"}) is None
    cached.assert_not_called()
    current = json.loads(os.environ["AGENTIHOOKS_CLAIM_FENCE"])
    monkeypatch.setenv("AGENTIHOOKS_CLAIM_FENCE", json.dumps({**current, "revision": current["revision"] + 1}))
    with (
        patch.object(claim_fence, "open_bus", return_value=bus) as connect,
        patch.object(claim_fence, "agent_pid", return_value=4321),
        patch.object(claim_fence, "terminate") as kill,
    ):
        assert claim_fence.check({"hook_event_name": "PreToolUse"}) == claim_fence.REVOKED
    connect.assert_called_once()
    kill.assert_called_once_with(4321)


def test_a_read_at_its_deadline_does_not_reach_the_bus() -> None:
    from hooks.context import claim_fence

    answers: list[bool] = []
    with (
        patch.object(claim_fence, "monotonic", return_value=5.0),
        patch.object(claim_fence, "owned", return_value=True) as owned,
    ):
        claim_fence.read("raw", 5.0, answers)
    owned.assert_not_called()
    assert answers == []


def test_the_bounded_read_runs_in_a_daemon_thread_joined_for_the_bound() -> None:
    from hooks.context import claim_fence

    with (
        patch.object(claim_fence, "Thread") as thread,
        patch.object(claim_fence, "monotonic", return_value=10.0),
    ):
        assert claim_fence.bounded("raw") is None
    thread.assert_called_once_with(target=claim_fence.read, args=("raw", 10.0 + claim_fence.BOUND, []), daemon=True)
    thread.return_value.start.assert_called_once_with()
    thread.return_value.join.assert_called_once_with(claim_fence.BOUND)


def test_the_cache_marker_lives_under_the_agentihooks_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from hooks.context import claim_fence

    digest = hashlib.sha256(b"raw").hexdigest()
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "state"))
    assert claim_fence.marker("raw") == tmp_path / "state" / "claim-fence" / digest
    monkeypatch.delenv("AGENTIHOOKS_HOME")
    with patch.object(claim_fence.Path, "home", return_value=tmp_path):
        assert claim_fence.marker("raw") == tmp_path / ".agentihooks" / "claim-fence" / digest


@pytest.mark.parametrize(("age", "expected"), [(-1.0, False), (0.0, True), (4.0, True), (5.0, False)])
def test_a_held_answer_is_fresh_only_inside_the_cache_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, age: float, expected: bool
) -> None:
    from hooks.context import claim_fence

    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    path = claim_fence.marker("raw")
    path.parent.mkdir(parents=True)
    path.touch()
    os.utime(path, (1000.0, 1000.0))
    with patch.object(claim_fence, "time", return_value=1000.0 + age):
        assert claim_fence.fresh("raw") is expected


@pytest.mark.parametrize("existing", [False, True])
def test_remembering_a_held_answer_creates_its_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, existing: bool
) -> None:
    from hooks.context import claim_fence

    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "a" / "b"))
    if existing:
        claim_fence.marker("raw").parent.mkdir(parents=True)
    claim_fence.remember("raw")
    assert claim_fence.marker("raw").is_file()


def test_remembering_on_an_unwritable_home_does_not_fail_the_hook(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from hooks.context import claim_fence

    home = tmp_path / "home"
    home.write_text("")
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(home))
    claim_fence.remember("raw")
    assert home.is_file()
