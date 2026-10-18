from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hooks.lifecycle import refresh
from scripts import init_agent


@pytest.mark.parametrize("status", [0, 23, 143])
@pytest.mark.parametrize("closing", [False, True])
def test_launcher_retains_native_exit_before_cleanup(tmp_path, monkeypatch, status, closing):
    from scripts import profile_telemetry

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    herdr = fake_bin / "herdr"
    herdr.write_text('#!/usr/bin/env bash\nprintf \'{"result":{"text":"native final warning\\\\n"}}\\n\'\n')
    herdr.chmod(0o700)
    shell = fake_bin / "shell"
    shell.write_text('#!/usr/bin/env bash\ntest -z "${AGENTIHOOKS_EXIT_RECORD+x}"\n')
    shell.chmod(0o700)
    command = f'printf "%s" "$AGENTIHOOKS_EXIT_RECORD" > "{tmp_path}/native-record-path"; '
    if closing:
        command += f'touch "{tmp_path}/agentihooks-claude-terminal/closing-$PPID"; '
    native = "kill -TERM -- -$$" if status == 143 else f"exit {status}"
    monkeypatch.setattr(init_agent, "_agent_command", lambda *args: (["bash", "-c", f"{command}{native}"], ""))
    monkeypatch.setattr(init_agent, "LAUNCH_GRACE_S", 0)
    monkeypatch.setattr(init_agent.operator_env, "source_line", lambda environ: "")
    monkeypatch.setattr(profile_telemetry, "installed_langfuse_env", lambda agent: {})
    environ = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "XDG_RUNTIME_DIR": str(tmp_path),
        "HERDR_PANE_ID": "pane-test",
        "SHELL": str(shell),
    }
    launcher, _ = init_agent._write_launcher(tmp_path, "native-test", "", [], environ)
    done = subprocess.run(
        ["bash", str(launcher)], env=environ, capture_output=True, text=True, timeout=30, start_new_session=True
    )
    records = list((Path.home() / ".agentihooks" / "session-exits").glob("*.json"))
    assert len(records) == 1, done.stderr
    assert Path((tmp_path / "native-record-path").read_text()) == records[0]
    record = json.loads(records[0].read_text())
    assert record["exit_status"] == status
    assert record["pane_tail"] == "native final warning\n"
    assert record["pane_id"] == "pane-test"
    assert record["name"] == "native-test"
    assert record["harness"] == "claude"
    assert done.returncode == 0
    assert not launcher.exists()


def test_restart_retains_exit_evidence_before_relaunch(tmp_path, monkeypatch):
    record = tmp_path / "native.json"
    monkeypatch.setenv("AGENTIHOOKS_EXIT_RECORD", str(record))
    monkeypatch.setattr(refresh, "live_session_ids", lambda: [])
    calls = []

    def sleep(delay):
        pytest.fail("an existing native exit record must not wait")

    monkeypatch.setattr(refresh.time, "sleep", sleep)

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "terminate-agent":
            record.write_text(json.dumps({"exit_status": 143, "pane_tail": "native stopped", "pane_id": "pane-test"}))
        else:
            saved = json.loads(record.read_text())
            assert saved["session_id"] == "sid-evidence"
            assert saved["exit_status"] == 143
            assert saved["pane_tail"] == "native stopped"
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(refresh.subprocess, "run", run)
    refresh.restart([["ah", "terminate-agent"], ["ah", "init-agent"]], tmp_path / "closing-1", "sid-evidence")
    assert len(calls) == 2


@pytest.mark.parametrize("failure", [None, "no-pane", "herdr", "os", "timeout"])
def test_exit_record_retains_capture_outcome(tmp_path, monkeypatch, failure):
    from scripts import herdr_host, session_exit

    environ = {"HERDR_PANE_ID": "pane-test", "HERDR_SOCKET_PATH": "test.sock"}
    if failure == "no-pane":
        environ = {}
    errors = {
        "herdr": herdr_host.HerdrError("read refused"),
        "os": OSError("read refused"),
        "timeout": subprocess.TimeoutExpired("herdr", 30),
    }

    def read(argv, env):
        assert argv == ["pane", "read", "pane-test", "--source", "recent", "--lines", "40", "--format", "text"]
        assert env == environ
        if failure:
            raise errors[failure]
        return {"text": "last native lines"}

    def redact(text, *, mode):
        assert mode == "strict"
        return f"safe:{text}"

    monkeypatch.setattr(herdr_host, "_cli", read)
    monkeypatch.setattr(session_exit, "redact", redact)
    monkeypatch.setattr(session_exit.time, "time", lambda: 42)
    path = tmp_path / "evidence" / "nested" / "native.json"
    session_exit.record_exit(path, 23, "claude", "master-test", environ)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved == {
        "exit_status": 23,
        "harness": "claude",
        "name": "master-test",
        "pane_id": "" if failure == "no-pane" else "pane-test",
        "pane_tail": "safe:" if failure else "safe:last native lines",
        "pane_error": "safe:no herdr pane"
        if failure == "no-pane"
        else f"safe:{errors[failure]}"
        if failure
        else "safe:",
        "ended_at": 42,
    }
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert not path.with_suffix(".pending").exists()


@pytest.mark.parametrize("home", ["explicit", "home", "default"])
def test_exit_records_use_durable_session_home(tmp_path, home):
    from scripts import session_exit

    environ = (
        {"AGENTIHOOKS_HOME": str(tmp_path)} if home == "explicit" else {"HOME": str(tmp_path)} if home == "home" else {}
    )
    expected = (
        tmp_path
        if home == "explicit"
        else tmp_path / ".agentihooks"
        if home == "home"
        else Path.home() / ".agentihooks"
    )
    assert session_exit.path_for(Path("native-launch.sh"), environ) == expected / "session-exits" / "native-launch.json"


@pytest.mark.parametrize("implicit", [False, True])
def test_exit_recorder_command_preserves_native_fields(tmp_path, monkeypatch, implicit):
    from scripts import session_exit

    path = tmp_path / "native.json"
    argv = [str(path), "23", "claude", "native-session"]
    monkeypatch.setattr(sys, "argv", ["recorder", *(argv if implicit else ["unrelated"])])
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    monkeypatch.setattr(session_exit.time, "time", lambda: 42)
    assert session_exit.main(None if implicit else argv) == 0
    assert json.loads(path.read_text()) == {
        "exit_status": 23,
        "harness": "claude",
        "name": "native-session",
        "pane_id": "",
        "pane_tail": "",
        "pane_error": "no herdr pane",
        "ended_at": 42,
    }


def test_restart_without_exit_recorder_preserves_the_running_session(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AGENTIHOOKS_EXIT_RECORD", raising=False)
    calls = []
    monkeypatch.setattr(refresh.subprocess, "run", lambda *args, **kwargs: calls.append(args))
    marker = tmp_path / "closing-1"
    refresh.restart([["ah", "terminate-agent"], ["ah", "init-agent"]], marker, "old-session")
    assert calls == []
    assert not marker.exists()
    assert "launcher has no exit recorder, restart skipped" in capsys.readouterr().err


@pytest.mark.parametrize("arrives", [False, True])
def test_restart_waits_for_native_evidence(tmp_path, monkeypatch, capsys, arrives):
    record = tmp_path / "native.json"
    monkeypatch.setenv("AGENTIHOOKS_EXIT_RECORD", str(record))
    monkeypatch.setattr(refresh, "live_session_ids", lambda: [])
    ticks = iter([0, 1, 34.9, 35, 35.1, 36])
    monkeypatch.setattr(refresh.time, "monotonic", lambda: next(ticks))
    sleeps = []

    def sleep(delay):
        assert delay == 0.1
        sleeps.append(delay)
        assert len(sleeps) <= (1 if arrives else 2)
        if arrives:
            record.write_text(json.dumps({"exit_status": 143, "pane_tail": "native stopped"}))

    monkeypatch.setattr(refresh.time, "sleep", sleep)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(refresh.subprocess, "run", run)
    refresh.restart([["ah", "terminate-agent"], ["ah", "init-agent"]], tmp_path / "closing-1", "sid")
    assert len(calls) == (2 if arrives else 1)
    assert len(sleeps) == (1 if arrives else 2)
    if arrives:
        assert json.loads(record.read_text())["session_id"] == "sid"
    else:
        assert "has no native exit record, resume skipped" in capsys.readouterr().err
