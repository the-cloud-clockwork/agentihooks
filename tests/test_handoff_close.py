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


def test_closing_terminates_this_session_after_marking_its_terminal_to_exit(monkeypatch, tmp_path):
    monkeypatch.setattr(handoff_close.shutil, "which", lambda name: "/bin/agentihooks")
    assert handoff_close.close_command("sid-1") == ["/bin/agentihooks", "terminate-agent", "sid-1", "--type", "claude"]
    ran = []
    monkeypatch.setattr(handoff_close.subprocess, "run", lambda argv, **kwargs: ran.append(argv))
    marker = tmp_path / "closing-42"
    handoff_close.close(["/bin/agentihooks", "terminate-agent", "sid-1"], marker)
    assert marker.exists() and ran == [["/bin/agentihooks", "terminate-agent", "sid-1"]]


def _stop(monkeypatch, tmp_path, status):
    scheduled = []
    monkeypatch.setattr("hooks.lifecycle.handoff_close._home", lambda: tmp_path)
    monkeypatch.setattr("hooks.context.broadcast._load_sessions", lambda: {"sid-1": {"status": status}})
    monkeypatch.setattr("hooks.context.account_sessions.agent_pid", lambda: 4242)
    monkeypatch.setattr(handoff_close, "_closing_marker", lambda pid, environ: tmp_path / f"closing-{pid}")
    monkeypatch.setattr("hooks._async.fork_and_call", lambda fn, *args, **kwargs: scheduled.append(args))
    monkeypatch.delenv(handoff_close.CLOSE_ENV, raising=False)
    return scheduled


def test_a_handed_off_stop_schedules_the_close_once(monkeypatch, tmp_path):
    scheduled = _stop(monkeypatch, tmp_path, "handed_off")
    assert handoff_close.on_stop({"session_id": "sid-1"}) is True
    assert handoff_close.on_stop({"session_id": "sid-1"}) is False
    assert len(scheduled) == 1
    command, marker = scheduled[0]
    assert command[-3:] == ["sid-1", "--type", "claude"] and marker == tmp_path / "closing-4242"


def test_a_live_session_stop_schedules_nothing(monkeypatch, tmp_path):
    scheduled = _stop(monkeypatch, tmp_path, "alive")
    assert handoff_close.on_stop({"session_id": "sid-1"}) is False
    assert scheduled == []
