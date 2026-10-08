from __future__ import annotations

import json
import os
import subprocess
import time

import init_agent
import pytest

from hooks.lifecycle import refresh
from scripts import deps_preflight


def _log_change(home, ts: float, affects: bool = True) -> None:
    with (home / "deps-installs.jsonl").open("a") as fh:
        fh.write(json.dumps({"ts": ts, "id": "x", "ok": True, "affects_sessions": affects}) + "\n")


@pytest.fixture
def home(tmp_path):
    return tmp_path


LAUNCHED = {refresh.LAUNCH_ENV: "1"}


def test_due_when_a_change_lands_after_the_process_started(home):
    _log_change(home, time.time() + 60)
    assert refresh.due({}, LAUNCHED, home, os.getpid()) is True


def test_not_due_when_the_process_started_after_the_change(home):
    _log_change(home, time.time() - 3600)
    assert refresh.due({}, LAUNCHED, home, os.getpid()) is False


def test_changes_that_do_not_affect_sessions_are_ignored(home):
    _log_change(home, time.time() + 60, affects=False)
    assert refresh.due({}, LAUNCHED, home, os.getpid()) is False


def test_hand_launched_sessions_are_never_restarted(home):
    _log_change(home, time.time() + 60)
    assert refresh.due({}, {}, home, os.getpid()) is False


def test_swarm_sessions_are_never_restarted(home):
    _log_change(home, time.time() + 60)
    assert refresh.due({}, {**LAUNCHED, "AGENTIHOOKS_SWARM": "sw"}, home, os.getpid()) is False


@pytest.mark.parametrize("payload", [{"agent_id": "a1"}, {"stop_hook_active": True}])
def test_subagent_and_reentrant_stops_are_ignored(home, payload):
    _log_change(home, time.time() + 60)
    assert refresh.due(payload, LAUNCHED, home, os.getpid()) is False


ORIGINAL = refresh.Original(
    session_id="sid-1",
    name="eng-a",
    cwd="/work/wt",
    account="acct2",
    pid=4242,
    profile="engineer",
    model="opus",
    effort="high",
)


def test_restart_kills_the_original_process_then_resumes_through_its_profile(monkeypatch):
    monkeypatch.setattr(refresh.shutil, "which", lambda name: "/bin/agentihooks")
    kill, launch = refresh.restart_commands(ORIGINAL)
    assert kill == ["/bin/agentihooks", "terminate-agent", "4242", "--type", "claude", "--force-shared"]
    assert launch == [
        "/bin/agentihooks",
        "init-agent",
        "--agent",
        "claude",
        "--dir",
        "/work/wt",
        "--name",
        "eng-a",
        "--prompt",
        refresh.NOTE,
        "--profile",
        "engineer",
        "--",
        "--route",
        "acct2",
        "--model",
        "opus",
        "--effort",
        "high",
        "--resume",
        "sid-1",
    ]


def test_original_reads_the_profile_model_and_effort_the_session_was_launched_with():
    env = {"AGENTIHOOKS_PROFILE": "engineer", "AGENTIHOOKS_RUN_MODEL": "opus", "AGENTIHOOKS_RUN_EFFORT": "high"}
    original = refresh.Original.of("sid-1", "eng-a", {"cwd": "/work/wt", "account": "acct2"}, 4242, env)
    assert original == ORIGINAL


DETACHED = {"stdin": subprocess.DEVNULL, "capture_output": True, "timeout": 300, "check": False}


def _restart(monkeypatch, tmp_path, kill_rc: int, live: list[str], consumed: bool = False) -> list[list[str]]:
    ran = []
    marker = tmp_path / "closing-1"

    def run(argv, **kwargs):
        assert kwargs == DETACHED
        ran.append(argv)
        if consumed:
            marker.unlink(missing_ok=True)
        return subprocess.CompletedProcess(argv, kill_rc if argv[1] == "terminate-agent" else 0, "", "refused")

    monkeypatch.setattr(refresh.subprocess, "run", run)
    monkeypatch.setattr(refresh, "live_session_ids", lambda: live)
    refresh.restart([["ah", "terminate-agent", "4242"], ["ah", "init-agent"]], marker, "sid-1")
    return ran


def test_restart_resumes_only_once_no_process_carries_the_session(monkeypatch, tmp_path):
    ran = _restart(monkeypatch, tmp_path, 0, ["sid-other"])
    assert ran == [["ah", "terminate-agent", "4242"], ["ah", "init-agent"]]
    assert (tmp_path / "closing-1").exists()


def test_a_refused_kill_never_launches_a_second_copy(monkeypatch, tmp_path, capsys):
    ran = _restart(monkeypatch, tmp_path, 2, [])
    assert ran == [["ah", "terminate-agent", "4242"]]
    assert not (tmp_path / "closing-1").exists()
    assert "session-refresh: sid-1 still running, resume skipped: 'refused'" in capsys.readouterr().err


def test_a_surviving_process_with_the_session_id_never_gets_a_resumed_copy(monkeypatch, tmp_path):
    ran = _restart(monkeypatch, tmp_path, 0, ["sid-1"], consumed=True)
    assert ran == [["ah", "terminate-agent", "4242"]]


def test_live_session_ids_lists_every_live_agent_session(monkeypatch):
    from scripts import terminate_agent

    found = [type("S", (), {"session_id": "sid-1"})(), type("S", (), {"session_id": "sid-2"})()]
    monkeypatch.setattr(terminate_agent, "sessions", lambda: found)
    assert refresh.live_session_ids() == ["sid-1", "sid-2"]


def test_on_stop_restarts_this_agent_through_its_launch_profile(monkeypatch, tmp_path):
    from hooks import _async
    from hooks.context import account_sessions, broadcast

    _log_change(tmp_path, time.time() + 60)
    calls = []
    monkeypatch.setattr(refresh, "_home", lambda: tmp_path)
    monkeypatch.setattr(account_sessions, "agent_pid", os.getpid)
    monkeypatch.setattr(broadcast, "_load_sessions", lambda: {"sid-1": {"cwd": "/work/wt", "account": "acct2"}})
    monkeypatch.setattr(
        refresh, "_closing_marker", lambda pid, environ: tmp_path / f"{pid}-{environ['AGENTIHOOKS_PROFILE']}"
    )
    monkeypatch.setattr(refresh.shutil, "which", lambda name: "/bin/agentihooks")
    monkeypatch.setattr(_async, "fork_and_call", lambda *args, **kwargs: calls.append((args, kwargs)))
    for key, value in {
        refresh.LAUNCH_ENV: "1",
        "AGENTIHOOKS_PROFILE": "engineer",
        "AGENTIHOOKS_RUN_MODEL": "opus",
        "AGENTIHOOKS_RUN_EFFORT": "high",
    }.items():
        monkeypatch.setenv(key, value)
    assert refresh.on_stop({"session_id": "sid-1"}) is True
    (func, commands, marker, session_id), kwargs = calls[0]
    expected = refresh.Original("sid-1", "sid-1", "/work/wt", "acct2", os.getpid(), "engineer", "opus", "high")
    assert (func, commands, marker, session_id) == (
        refresh.restart,
        refresh.restart_commands(expected),
        tmp_path / f"{os.getpid()}-engineer",
        "sid-1",
    )
    assert kwargs == {"timeout_sec": 600, "task_name": "session-refresh"}
    assert refresh.on_stop({"session_id": "sid-1"}) is False


def test_launcher_marks_the_launch_and_closes_its_tab_after_a_refresh(tmp_path):
    env = {"XDG_RUNTIME_DIR": str(tmp_path), "SHELL": "/bin/bash"}
    launcher, _ = init_agent._write_launcher(tmp_path, "eng-a", "", [], env)
    text = launcher.read_text()
    assert text.index("export AGENTIHOOKS_TERMINAL_LAUNCH=1") < text.index(" claude --")
    assert text.index("closing-$$") < text.index("exec /bin/bash -l")


def test_mark_changed_is_seen_as_a_session_affecting_change(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    before = time.time()
    assert deps_preflight.main(["mark-changed", "--reason", "mcp-registration"]) == 0
    assert refresh.latest_affecting_change(tmp_path) >= before
