"""The intent gate through the real hook executor: a failed verdict denies the merge and done, a fresh pending one
denies with the wait, a stale pending one passes counted, and observe only logs."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ME, SLUG, SID, TASK = "engineer@100001-0001", "demo", "sid-intent", "t1"
MATCHER = "bash.gh+bash.agentihooks"
PACKAGE = ".".join(("scripts", "gates"))
SHIM = (
    f"import runpy\nimport sys\n\nsys.argv = ['gate', 'intent']\nrunpy.run_module({PACKAGE!r}, run_name='__main__')\n"
)
MERGE = {"command": "gh pr merge 12 --squash", "description": "merge"}
DONE = {"command": f"agentihooks swarm {SLUG} done --pr https://github.com/o/r/pull/12", "description": "done"}
VIEW = {"command": "gh pr view 12", "description": "read"}


@pytest.fixture
def hook(tmp_path):
    home, bundle = tmp_path / "ahome", tmp_path / "bundle"
    conditions = bundle / ".claude" / "conditions"
    conditions.mkdir(parents=True)
    home.mkdir()
    (home / "state.json").write_text(json.dumps({"bundle": {"path": str(bundle)}}))
    (conditions / f"pre-{MATCHER}-intent.py").write_text(SHIM)
    gates = tmp_path / ".agentihooks" / "swarm" / SLUG / "gates"
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "PYTHONPATH": str(ROOT),
        "AGENTIHOOKS_HOME": str(home),
        "AGENTIHOOKS_TARGET": "claude",
        "AGENTIHOOKS_DISABLE_BYPASS_LOOKUP": "1",
        "CONDITIONS_ENABLED": "true",
        "BRAIN_ENABLED": "false",
        "BROADCAST_ENABLED": "false",
        "REDIS_URL": "redis://127.0.0.1:1/0",
        "AGENTIHOOKS_SWARM_REDIS_URL": "redis://127.0.0.1:1/0",
        "LEDGER_DIR": str(tmp_path / "ledgers"),
        "LEDGER_PORT": "1",
        "AGENTIHOOKS_SWARM": SLUG,
        "AGENTIHOOKS_AGENT_NAME": ME,
        "AGENTIHOOKS_SWARM_LANE": "eng",
        "AGENTIHOOKS_SWARM_TASK": TASK,
        "AGENTIHOOKS_GATE_INTENT": "enforce",
    }

    def run(tool_input=MERGE, extra=None):
        payload = {
            "hook_event_name": "PreToolUse",
            "session_id": SID,
            "permission_mode": "bypassPermissions",
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": tool_input,
        }
        return subprocess.run(
            [sys.executable, "-m", "hooks"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=ROOT,
            env={**env, **(extra or {})},
            timeout=60,
        )

    def verdict(value, reason, age_ms=0):
        path = gates / "intent" / TASK
        path.parent.mkdir(parents=True, exist_ok=True)
        at = int(time.time() * 1000) - age_ms
        path.write_text(json.dumps({"verdict": value, "reason": reason, "at": at}))

    def rows():
        path = gates / "log.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    run.verdict, run.rows = verdict, rows
    return run


def test_a_failed_verdict_denies_merge_and_done_and_lets_other_gh_calls_through(hook):
    hook.verdict("fail", "the phase can use this change at probability 0.05, under 0.3")
    for call in (MERGE, DONE):
        denied = hook(call)
        assert denied.returncode == 2, denied.stdout + denied.stderr
        assert (
            f"intent check failed for task {TASK}: the phase can use this change at probability 0.05" in denied.stderr
        )
        assert f"agentihooks swarm {SLUG} pr <url> for a new check" in denied.stderr
    assert hook(VIEW).returncode == 0
    assert [(r["gate"], r["kind"], r["agent"], r["task"]) for r in hook.rows()] == [("intent", "deny", ME, TASK)] * 2


def test_a_passed_verdict_lets_the_merge_through(hook):
    hook.verdict("pass", "the phase can use it as delivered at probability 0.90")
    assert hook().returncode == 0
    assert hook.rows() == []


def test_a_fresh_pending_verdict_denies_and_a_stale_one_passes_counted(hook):
    hook.verdict("pending", "intent check running")
    denied = hook()
    assert denied.returncode == 2
    assert "intent check running for task t1" in denied.stderr
    hook.verdict("pending", "intent check running", age_ms=121_000)
    assert hook().returncode == 0
    assert [r["kind"] for r in hook.rows()] == ["deny", "count"]


def test_the_default_observe_mode_logs_the_would_be_deny(hook):
    hook.verdict("fail", "no")
    assert hook(extra={"AGENTIHOOKS_GATE_INTENT": ""}).returncode == 0
    assert [r["kind"] for r in hook.rows()] == ["observe"]
