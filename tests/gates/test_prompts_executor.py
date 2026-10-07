"""The prompt guard through the real hook executor: the package role's condition shim running the gate entry point."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHIM = "pre-bash.rm+bash.rmdir+bash.bash+bash.sh+bash.zsh+bash.eval-prompt_guard.py"
OBSERVED = "mkdir -p {cwd}/x && cd {cwd} && rm -rf * ;"


@pytest.fixture(params=["claude", "codex"])
def hook(tmp_path, request):
    home, bundle = tmp_path / "ahome", tmp_path / "bundle"
    conditions = bundle / ".claude" / "conditions"
    conditions.mkdir(parents=True)
    home.mkdir()
    (home / "state.json").write_text(json.dumps({"bundle": {"path": str(bundle)}}))
    shim = ROOT / "profiles" / "package" / "roles" / "engineer" / ".claude" / "conditions" / SHIM
    (conditions / SHIM).write_text(shim.read_text())
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "AGENTIHOOKS_HOME": str(home),
        "AGENTIHOOKS_TARGET": request.param,
        "AGENTIHOOKS_DISABLE_BYPASS_LOOKUP": "1",
        "CONDITIONS_ENABLED": "true",
        "BRAIN_ENABLED": "false",
        "BROADCAST_ENABLED": "false",
        "REDIS_URL": "redis://127.0.0.1:1/0",
        "AGENTIHOOKS_SWARM": "demo",
        "AGENTIHOOKS_AGENT_NAME": "engineer@1-1",
    }

    def run(command):
        payload = {
            "hook_event_name": "PreToolUse",
            "session_id": "sid-prompts",
            "tool_name": "Bash",
            "tool_input": {"command": command.format(cwd=tmp_path), "description": "probe"},
            "permission_mode": "bypassPermissions",
            "cwd": str(tmp_path),
        }
        return subprocess.run(
            [sys.executable, "-m", "hooks"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=ROOT,
            env=env,
            timeout=60,
        )

    return run


def test_the_observed_command_is_denied_before_the_harness_asks(hook):
    result = hook(OBSERVED)
    assert result.returncode == 2
    assert "agentihooks scratch rm" in result.stderr


def test_a_wrapped_rm_reaches_the_gate(hook):
    result = hook("sudo rm -rf $TARGET")
    assert result.returncode == 2
    assert "known only when it runs" in result.stderr


def test_a_named_file_passes(hook):
    result = hook("rm -f {cwd}/x/notes.txt")
    assert result.returncode == 0, result.stderr
    assert "agentihooks scratch rm" not in result.stderr


def test_a_subagent_script_running_a_variable_command_is_denied(hook):
    result = hook("bash -e -c 'replay() {{ shift; if ! \"$@\" > /dev/null; then echo failed; fi; }}; replay bad false'")
    assert result.returncode == 2
    assert "shell -c script" in result.stderr
