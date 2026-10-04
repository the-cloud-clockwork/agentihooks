from __future__ import annotations

import json
import os
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


def test_restart_kills_then_resumes_with_name_account_and_dir(monkeypatch):
    monkeypatch.setattr(refresh.shutil, "which", lambda name: "/bin/agentihooks")
    kill, launch = refresh.restart_commands("sid-1", "eng-a", "/work/wt", "acct2")
    assert kill == ["/bin/agentihooks", "terminate-agent", "sid-1", "--type", "claude"]
    assert launch == [
        "/bin/agentihooks",
        "init-agent",
        "--dir",
        "/work/wt",
        "--name",
        "eng-a",
        "--prompt",
        refresh.NOTE,
        "--",
        "--route",
        "acct2",
        "--resume",
        "sid-1",
    ]


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
