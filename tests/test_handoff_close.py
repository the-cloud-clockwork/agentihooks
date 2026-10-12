from __future__ import annotations

import pytest

from hooks.lifecycle import handoff_close


@pytest.mark.parametrize(
    ("payload", "environ", "status", "expected"),
    [
        ({}, {}, "handed_off", True),
        ({}, {}, "alive", False),
        ({}, {handoff_close.CLOSE_ENV: "0"}, "handed_off", False),
        ({"agent_id": "a1"}, {}, "handed_off", False),
        ({"stop_hook_active": True}, {}, "handed_off", False),
    ],
)
def test_only_a_handed_off_session_closes_at_its_stop(payload, environ, status, expected):
    assert handoff_close.due(payload, environ, status) is expected


@pytest.mark.parametrize(("returncode", "present", "kept"), [(0, True, False), (0, False, False), (1, True, True)])
def test_closing_terminates_this_session_after_marking_its_terminal_to_exit(
    monkeypatch, tmp_path, returncode, present, kept
):
    monkeypatch.setattr(handoff_close.shutil, "which", lambda name: "/bin/agentihooks")
    assert handoff_close.close_command(4242) == ["/bin/agentihooks", "terminate-agent", "4242", "--type", "any"]
    ran = []

    def run(argv, **kwargs):
        ran.append((argv, kwargs))
        return handoff_close.subprocess.CompletedProcess(argv, returncode)

    monkeypatch.setattr(handoff_close.subprocess, "run", run)
    marker, record = tmp_path / "closing-42", tmp_path / "handed-off-42-sid-1"
    if present:
        record.touch()
    handoff_close.close(["/bin/agentihooks", "terminate-agent", "42"], marker, record)
    options = {"stdin": handoff_close.subprocess.DEVNULL, "capture_output": True, "timeout": 120, "check": False}
    assert marker.exists() and ran == [(["/bin/agentihooks", "terminate-agent", "42"], options)]
    assert record.exists() is kept


def _stop(monkeypatch, tmp_path, status, pid=4242):
    scheduled = []
    monkeypatch.setattr("hooks.lifecycle.handoff_close._home", lambda: tmp_path)
    monkeypatch.setattr("hooks.context.broadcast._load_sessions", lambda: {"sid-1": {"status": status}})
    monkeypatch.setattr("hooks.context.account_sessions.agent_pid", lambda: pid)
    monkeypatch.setattr(handoff_close, "_closing_marker", lambda pid, environ: tmp_path / f"closing-{pid}")
    monkeypatch.setattr("hooks._async.fork_and_call", lambda fn, *args, **kwargs: scheduled.append(args))
    monkeypatch.delenv(handoff_close.CLOSE_ENV, raising=False)
    return scheduled


def test_a_handed_off_stop_schedules_the_close_once(monkeypatch, tmp_path):
    scheduled = _stop(monkeypatch, tmp_path, "handed_off")
    assert handoff_close.on_stop({"session_id": "sid-1"}) is True
    assert handoff_close.on_stop({"session_id": "sid-1"}) is False
    assert len(scheduled) == 1
    assert scheduled[0] == (
        [handoff_close.close_command(4242)[0], "terminate-agent", "4242", "--type", "any"],
        tmp_path / "closing-4242",
        tmp_path / "handed-off-4242-sid-1",
    )


def test_a_live_session_stop_schedules_nothing(monkeypatch, tmp_path):
    scheduled = _stop(monkeypatch, tmp_path, "alive")
    assert handoff_close.on_stop({"session_id": "sid-1"}) is False
    assert scheduled == []


def test_an_unregistered_session_stop_schedules_nothing(monkeypatch, tmp_path):
    scheduled = _stop(monkeypatch, tmp_path, "handed_off")
    assert handoff_close.on_stop({"session_id": "sid-unknown"}) is False
    assert scheduled == []


def test_a_codex_process_whose_resumed_successor_took_its_session_still_closes(monkeypatch, tmp_path):
    scheduled = _stop(monkeypatch, tmp_path, "alive")
    handoff_close.handoff_record(4242, "sid-1").touch()
    assert handoff_close.handoff_record(4242, "sid-1") == tmp_path / "handed-off-4242-sid-1"
    assert handoff_close.on_stop({"session_id": "sid-1"}) is True
    assert [command[-3:] for command, _, _ in scheduled] == [["4242", "--type", "any"]]
    assert handoff_close.handoff_record(4242, "sid-1").exists()


def test_a_handoff_recorded_for_another_process_or_session_closes_nothing(monkeypatch, tmp_path):
    scheduled = _stop(monkeypatch, tmp_path, "alive")
    handoff_close.handoff_record(4243, "sid-1").touch()
    handoff_close.handoff_record(4242, "sid-2").touch()
    assert handoff_close.on_stop({"session_id": "sid-1"}) is False
    assert scheduled == []


def test_each_process_of_a_resumed_conversation_closes_once(monkeypatch, tmp_path):
    first = _stop(monkeypatch, tmp_path, "handed_off")
    assert handoff_close.on_stop({"session_id": "sid-1"}) is True
    second = _stop(monkeypatch, tmp_path, "handed_off", pid=5151)
    assert handoff_close.on_stop({"session_id": "sid-1"}) is True
    assert handoff_close.on_stop({"session_id": "sid-1"}) is False
    assert [command[-3] for command, _, _ in first + second] == ["4242", "5151"]
