"""The identity gate through the real hook executor: a condition shim running the gate entry point."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ME, OTHER, SLUG = "engineer@1-1", "engineer@1-2", "demo"
SHIM = "from scripts.gates.entry import main\n\nraise SystemExit(main(['identity']))\n"


@pytest.fixture
def hook(tmp_path):
    home, bundle = tmp_path / "ahome", tmp_path / "bundle"
    conditions = bundle / ".claude" / "conditions"
    conditions.mkdir(parents=True)
    home.mkdir()
    (home / "state.json").write_text(json.dumps({"bundle": {"path": str(bundle)}}))
    (conditions / "pre-bash-identity.py").write_text(SHIM)
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "AGENTIHOOKS_HOME": str(home),
        "AGENTIHOOKS_TARGET": "claude",
        "AGENTIHOOKS_DISABLE_BYPASS_LOOKUP": "1",
        "CONDITIONS_ENABLED": "true",
        "BRAIN_ENABLED": "false",
        "BROADCAST_ENABLED": "false",
        "REDIS_URL": "redis://127.0.0.1:1/0",
        "AGENTIHOOKS_SWARM": SLUG,
        "AGENTIHOOKS_AGENT_NAME": ME,
    }

    def run(command):
        payload = {
            "hook_event_name": "PreToolUse",
            "session_id": "sid-identity",
            "tool_name": "Bash",
            "tool_input": {"command": command, "description": "probe"},
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


def test_another_agents_name_is_denied(hook):
    proc = hook(f"agentihooks ledger --slug {SLUG} --as {OTHER} say hi")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert (
        f"[condition pre-bash-identity.py] this session is {ME} in swarm {SLUG} and cannot act as {OTHER}"
        in proc.stderr
    )


def test_own_name_passes(hook):
    proc = hook(f"agentihooks ledger --slug {SLUG} --as {ME} say hi")
    assert proc.returncode == 0, proc.stderr
    assert "cannot act as" not in proc.stdout + proc.stderr
