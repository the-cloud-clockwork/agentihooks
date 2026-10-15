from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import init_agent
import pytest

from hooks.lifecycle import refresh


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
    shell.write_text('#!/usr/bin/env bash\ntest -s "$AGENTIHOOKS_EXIT_RECORD"\n')
    shell.chmod(0o700)
    command = f'touch "{tmp_path}/agentihooks-claude-terminal/closing-$PPID"; ' if closing else ""
    monkeypatch.setattr(init_agent, "_agent_command", lambda *args: (["bash", "-c", f"{command}exit {status}"], ""))
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
    done = subprocess.run(["bash", str(launcher)], env=environ, capture_output=True, text=True, timeout=30)
    records = list((Path.home() / ".agentihooks" / "session-exits").glob("*.json"))
    assert len(records) == 1, done.stderr
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
