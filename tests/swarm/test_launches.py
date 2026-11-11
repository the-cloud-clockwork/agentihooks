import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from hooks.proc import processes
from scripts import select_profile
from scripts.swarm import launches, reaper
from scripts.swarm.store import AgentRecord
from tests.test_swarm_v2_runtime import herdr_runtime

NAME = "engineer@a1b2c3-0001"


def test_record_writes_this_process_and_its_start_under_the_agent_name(tmp_path):
    launches.record({"AGENTIHOOKS_SWARM": "crew", "AGENTIHOOKS_AGENT_NAME": NAME}, tmp_path)
    data = json.loads((tmp_path / "launches" / f"{NAME}.json").read_text())
    assert data == {"pid": os.getpid(), "start_time": processes()[os.getpid()].start_time}
    assert launches.find(NAME, tmp_path).pid == os.getpid()


def test_a_second_launch_under_the_same_name_replaces_the_record(tmp_path):
    (tmp_path / "launches").mkdir()
    (tmp_path / "launches" / f"{NAME}.json").write_text('{"pid": 1, "start_time": 1}')
    launches.record({"AGENTIHOOKS_SWARM": "crew", "AGENTIHOOKS_AGENT_NAME": NAME}, tmp_path)
    assert launches.find(NAME, tmp_path).pid == os.getpid()


def test_record_writes_nothing_when_its_own_process_cannot_be_read(tmp_path, monkeypatch):
    monkeypatch.setattr(launches, "processes", lambda: {})
    launches.record({"AGENTIHOOKS_SWARM": "crew", "AGENTIHOOKS_AGENT_NAME": NAME}, tmp_path)
    assert not (tmp_path / "launches").exists()


@pytest.mark.parametrize("environ", [{"AGENTIHOOKS_AGENT_NAME": NAME}, {"AGENTIHOOKS_SWARM": "crew"}, {}])
def test_record_writes_nothing_outside_a_named_swarm_launch(tmp_path, environ):
    launches.record(environ, tmp_path)
    assert not (tmp_path / "launches").exists()


def test_record_defaults_to_the_swarm_home(tmp_path):
    launches.record({"AGENTIHOOKS_SWARM": "crew", "AGENTIHOOKS_AGENT_NAME": NAME})
    assert launches.find(NAME).pid == os.getpid()
    assert (launches.home() / "launches" / f"{NAME}.json").is_file()
    assert launches.home() == Path.home() / ".agentihooks" / "swarm"


@pytest.mark.parametrize(
    "content", [None, "not json", "{}", "[]", '{"pid": %d, "start_time": 1}', '{"pid": 999999999, "start_time": 1}']
)
def test_find_returns_nothing_for_a_missing_unreadable_restarted_or_ended_launch(tmp_path, content):
    if content is not None:
        (tmp_path / "launches").mkdir()
        text = content % os.getpid() if "%d" in content else content
        (tmp_path / "launches" / f"{NAME}.json").write_text(text)
    assert launches.find(NAME, tmp_path) is None


def _sleeping_popen(launched: list[subprocess.Popen]) -> Callable[..., subprocess.Popen]:
    real = subprocess.Popen

    def popen(command: list[str], env: dict[str, str]) -> subprocess.Popen:
        child = real([sys.executable, "-c", "import time; time.sleep(30)"], env=env)
        launched.append(child)
        return child

    return popen


@pytest.mark.feature("select-profile-records-the-agent-process")
def test_select_profile_records_the_launched_agent_process_and_retire_ends_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(launches, "home", lambda: tmp_path)
    monkeypatch.setattr(select_profile, "prepare", lambda *args: ({}, []))
    herdr, _ = herdr_runtime(tmp_path, monkeypatch, panes={})
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "crew")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", NAME)
    ended = []
    monkeypatch.setattr(herdr, "end", lambda *args: ended.append(args) or reaper.Outcome())
    launched: list[subprocess.Popen] = []
    seen = []
    start = _sleeping_popen(launched)

    def popen(command: list[str], env: dict[str, str]) -> subprocess.Popen:
        child = start(command, env)
        wait = child.wait

        def waited(timeout: float | None = None) -> int:
            if not seen:
                seen.append(launches.find(NAME))
                assert herdr.retire(replace(AgentRecord(NAME, "eng", "t1"), pane_id="", state="finished"))
                child.kill()
            return wait(timeout)

        child.wait = waited
        return child

    monkeypatch.setattr(select_profile.subprocess, "Popen", popen)
    try:
        assert select_profile.main(["engineer"]) == -signal.SIGKILL
        agent = launched[0]
        assert agent.pid != os.getpid()
        assert seen[0] is not None and seen[0].pid == agent.pid
        assert ended == [(NAME, agent.pid, [], seen[0].start_time)]
    finally:
        for child in launched:
            child.kill()


@pytest.mark.feature("select-profile-records-the-agent-process")
def test_select_profile_ends_the_agent_when_its_launch_cannot_be_recorded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(select_profile, "prepare", lambda *args: ({}, []))
    monkeypatch.setattr(launches, "record", Mock(side_effect=OSError("disk full")))
    launched: list[subprocess.Popen] = []
    monkeypatch.setattr(select_profile.subprocess, "Popen", _sleeping_popen(launched))
    try:
        with pytest.raises(OSError, match="disk full"):
            select_profile.main(["engineer"])
        assert launched[0].wait(timeout=5) == -signal.SIGKILL
    finally:
        for child in launched:
            child.kill()


def test_select_profile_dry_run_records_nothing(monkeypatch):
    monkeypatch.setattr(select_profile, "prepare", lambda *args: ({}, []))
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "crew")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", NAME)
    assert select_profile.main(["engineer", "--dry-run"]) == 0
    assert launches.find(NAME) is None


def test_retire_pane_ends_the_recorded_launch_of_an_agent_with_no_pid_or_pane(tmp_path, monkeypatch):
    herdr, calls = herdr_runtime(tmp_path, monkeypatch, panes={})
    herdr.end = reaper.retire
    monkeypatch.setattr(herdr, "live_names", lambda: {NAME})
    monkeypatch.setattr("scripts.swarm.runtime._named_process", lambda agent: None)
    environment = {**os.environ, "AGENTIHOOKS_AGENT_NAME": NAME}
    launcher = "import subprocess, sys; h = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
    child = subprocess.Popen(
        [sys.executable, "-c", launcher + " print(h.pid, flush=True); h.wait()"],
        env=environment,
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        harness = int(child.stdout.readline())
        (tmp_path / "launches").mkdir()
        record = {"pid": child.pid, "start_time": processes()[child.pid].start_time}
        (tmp_path / "launches" / f"{NAME}.json").write_text(json.dumps(record))
        agent = replace(AgentRecord(NAME, "eng", "t1"), pane_id="", state="finished")
        assert herdr.retire_pane(agent)
        assert child.wait(timeout=5) == -signal.SIGTERM
        for _ in range(100):
            ended = processes().get(harness)
            if ended is None or ended.state == "Z":
                break
            time.sleep(0.05)
        assert ended is None or ended.state == "Z"
        assert not any(call[:2] == ["pane", "close"] for call in calls)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


def test_retire_pane_passes_the_recorded_launch_start(tmp_path, monkeypatch):
    herdr, _ = herdr_runtime(tmp_path, monkeypatch, panes={})
    ended = []
    herdr.end = lambda *args: ended.append(args) or reaper.Outcome()
    monkeypatch.setattr(herdr, "live_names", lambda: {NAME})
    monkeypatch.setattr("scripts.swarm.runtime._named_process", lambda agent: None)
    launches.record({"AGENTIHOOKS_SWARM": "crew", "AGENTIHOOKS_AGENT_NAME": NAME}, tmp_path)
    agent = replace(AgentRecord(NAME, "eng", "t1"), pane_id="w7:p7", state="finished")
    assert herdr.retire_pane(agent)
    assert ended == [(NAME, os.getpid(), [], processes()[os.getpid()].start_time)]


@pytest.mark.parametrize("validated", [False, True])
def test_legacy_retire_ends_the_recorded_launch_only_when_no_pid_was_validated(tmp_path, monkeypatch, validated):
    herdr, _ = herdr_runtime(tmp_path, monkeypatch, panes={})
    ended = []
    herdr.end = lambda *args: ended.append(args) or reaper.Outcome()
    launches.record({"AGENTIHOOKS_SWARM": "crew", "AGENTIHOOKS_AGENT_NAME": NAME}, tmp_path)
    decision = {"validation": {"pid": 4321}} if validated else {}
    agent = replace(AgentRecord(NAME, "eng", "t1"), pane_id="", profile_decision=decision)
    assert herdr.retire(agent)
    own = (os.getpid(), processes()[os.getpid()].start_time)
    assert ended == [(NAME, 4321, [], 0) if validated else (NAME, own[0], [], own[1])]
