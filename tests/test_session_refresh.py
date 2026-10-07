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


def _restart(monkeypatch, tmp_path, kill_rc: int, live: list[str]) -> list[list[str]]:
    ran = []

    def run(argv, **kwargs):
        ran.append(argv)
        return subprocess.CompletedProcess(argv, kill_rc if argv[1] == "terminate-agent" else 0)

    monkeypatch.setattr(refresh.subprocess, "run", run)
    monkeypatch.setattr(refresh, "live_session_ids", lambda: live)
    marker = tmp_path / "closing-1"
    refresh.restart([["ah", "terminate-agent", "4242"], ["ah", "init-agent"]], marker, "sid-1")
    return ran


def test_restart_resumes_only_once_no_process_carries_the_session(monkeypatch, tmp_path):
    ran = _restart(monkeypatch, tmp_path, 0, ["sid-other"])
    assert ran == [["ah", "terminate-agent", "4242"], ["ah", "init-agent"]]


def test_a_refused_kill_never_launches_a_second_copy(monkeypatch, tmp_path):
    ran = _restart(monkeypatch, tmp_path, 2, [])
    assert ran == [["ah", "terminate-agent", "4242"]]
    assert not (tmp_path / "closing-1").exists()


def test_a_surviving_process_with_the_session_id_never_gets_a_resumed_copy(monkeypatch, tmp_path):
    ran = _restart(monkeypatch, tmp_path, 0, ["sid-1"])
    assert ran == [["ah", "terminate-agent", "4242"]]


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
