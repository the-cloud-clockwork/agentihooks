import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts.gates import first_read

ROOT = Path(__file__).resolve().parents[2]
CONDITIONS = ROOT / "profiles" / "package" / "conditions"
RUNNER = """
import json
import sys
from hooks.context import conditions
from hooks.targets.normalizer import normalize_payload
payload = normalize_payload(json.loads(sys.stdin.read()))
step = sys.argv[1]
effect = getattr(conditions, step + '_effect')(payload)
print(json.dumps({'block': getattr(effect, 'block', None)}))
"""


def run_hook(tmp_path: Path, target: str, step: str, payload: dict[str, Any], mode: str = "enforce") -> dict[str, Any]:
    home = tmp_path / "ahome"
    home.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "PYTHONPATH": str(ROOT),
        "AGENTIHOOKS_HOME": str(home),
        "AGENTIHOOKS_TARGET": target,
        "AGENTIHOOKS_AGENT_NAME": "engineer@123-456",
        "AGENTIHOOKS_SWARM": "proof",
        "AGENTIHOOKS_DISABLE_BYPASS_LOOKUP": "1",
        "AGENTIHOOKS_GATE_FIRST_READ": mode,
        "CONDITIONS_ENABLED": "true",
        "BRAIN_ENABLED": "false",
        "BROADCAST_ENABLED": "false",
        "REDIS_URL": "unix:///nonexistent-first-read-redis.sock",
        "LEDGER_DIR": str(tmp_path / "ledgers"),
        "LEDGER_PORT": "1",
    }
    result = subprocess.run(
        [sys.executable, "-c", RUNNER, step],
        input=json.dumps({"session_id": "proof-session", "cwd": str(tmp_path), **payload}),
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.feature("first-read-gate")
@pytest.mark.parametrize(
    ("target", "tool", "args"),
    [
        ("claude", "Bash", {"command": "pwd"}),
        ("codex", "exec_command", {"cmd": "pwd"}),
        ("codex", "shell", {"command": ["pwd"]}),
        ("codex", "unified_exec", {"cmd": "pwd"}),
    ],
)
def test_enforce_blocks_native_shell_calls(tmp_path: Path, target: str, tool: str, args: dict[str, Any]) -> None:
    effect = run_hook(tmp_path, target, "pre", {"tool_name": tool, "tool_input": args})
    assert effect["block"] == "Run agentihooks seat brief before using other tools in this seated session."


@pytest.mark.feature("first-read-gate")
@pytest.mark.parametrize("target", ["claude", "codex"])
def test_observe_allows_and_records_both_harnesses(tmp_path: Path, target: str) -> None:
    effect = run_hook(tmp_path, target, "pre", {"tool_name": "Bash", "tool_input": {"command": "pwd"}}, "observe")
    assert effect["block"] is None
    rows = list(tmp_path.rglob("log.jsonl"))
    assert rows
    row = json.loads(rows[0].read_text().splitlines()[-1])
    assert (row["gate"], row["kind"], row["tool"]) == ("first-read", "observe", "Bash")


@pytest.mark.feature("first-read-gate")
def test_off_preserves_unrestricted_calls(tmp_path: Path) -> None:
    effect = run_hook(tmp_path, "claude", "pre", {"tool_name": "Write"}, "off")
    assert effect["block"] is None
    assert not list(tmp_path.rglob("log.jsonl"))


@pytest.mark.feature("first-read-gate")
@pytest.mark.parametrize("target", ["claude", "codex"])
def test_condition_lifecycle_marks_and_resets(tmp_path: Path, target: str) -> None:
    command = {
        "tool_name": "Bash",
        "tool_input": {"command": "agentihooks seat brief"},
        "tool_response": {"exit_code": 0},
    }
    run_hook(tmp_path, target, "post", command)
    marker = tmp_path / "ahome" / "first-read" / "proof-session"
    assert marker.read_text() == "1"
    assert run_hook(tmp_path, target, "pre", {"tool_name": "Write"})["block"] is None
    run_hook(tmp_path, target, "start", {"source": "compact"})
    assert not marker.exists()
    assert run_hook(tmp_path, target, "pre", {"tool_name": "Write"})["block"]


@pytest.mark.feature("first-read-gate")
def test_shell_shim_fast_exits_without_python(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shim = CONDITIONS / "pre-any-first_read.sh"
    env = {
        **os.environ,
        "PATH": str(tmp_path),
        "AGENTIHOOKS_SWARM": "",
        "AGENTIHOOKS_AGENT_NAME": "",
        "AGENTIHOOKS_QUICK_SEAT": "",
    }
    assert subprocess.run(["/bin/bash", str(shim)], env=env, capture_output=True, timeout=3).returncode == 0
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "ahome"))
    marker = first_read.marker_path("proof-session")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("1")
    env.update(
        AGENTIHOOKS_SWARM="proof",
        AGENTIHOOKS_AGENT_NAME="engineer@1",
        AH_SESSION_ID="proof-session",
        AGENTIHOOKS_HOME=str(tmp_path / "ahome"),
    )
    assert subprocess.run(["/bin/bash", str(shim)], env=env, capture_output=True, timeout=3).returncode == 0
