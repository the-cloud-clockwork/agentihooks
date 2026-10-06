"""The proof budget through the real hook executor: sub-agent launches and continuations, and CI reruns."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ME, SLUG, SID, HEAD = "engineer@1-1", "demo", "sid-budget", "c" * 40
PACKAGE = ".".join(("scripts", "gates"))
SHIM = (
    "import importlib\nimport sys\n\n"
    f"entry = importlib.import_module({PACKAGE + '.entry'!r})\n"
    f"subagents = importlib.import_module({PACKAGE + '.subagents'!r})\n"
    "entry.GATES['subagents'] = subagents.SubagentBudget(launches=2, continuations=2)\n"
    "raise SystemExit(entry.main([sys.argv[0].rsplit('-', 1)[-1][:-3]]))\n"
)
FAILED = {"head_sha": HEAD, "conclusion": "failure", "runner_name": "GitHub Actions 7"}
NO_RUNNER = {"head_sha": HEAD, "conclusion": "cancelled", "runner_name": None}
FAKE_GH = (
    "#!" + sys.executable + "\n"
    "import json, sys\n"
    f"failed, no_runner = {FAILED!r}, {NO_RUNNER!r}\n"
    "path = sys.argv[2]\n"
    "if '/jobs/' in path:\n"
    "    print(json.dumps(failed))\n"
    "else:\n"
    "    print(json.dumps({'jobs': [no_runner if '/runs/9/' in path else failed]}))\n"
)


@pytest.fixture
def hook(tmp_path):
    home, bundle, bin_dir = tmp_path / "ahome", tmp_path / "bundle", tmp_path / "bin"
    conditions = bundle / ".claude" / "conditions"
    for folder in (conditions, home, bin_dir):
        folder.mkdir(parents=True)
    (home / "state.json").write_text(json.dumps({"bundle": {"path": str(bundle)}}))
    (conditions / "pre-agent+task+sendmessage-subagents.py").write_text(SHIM)
    (conditions / "pre-bash.gh-reruns.py").write_text(SHIM)
    (bin_dir / "gh").write_text(FAKE_GH)
    (bin_dir / "gh").chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "PYTHONPATH": str(ROOT),
        "AGENTIHOOKS_HOME": str(home),
        "AGENTIHOOKS_TARGET": "claude",
        "AGENTIHOOKS_DISABLE_BYPASS_LOOKUP": "1",
        "CONDITIONS_ENABLED": "true",
        "BRAIN_ENABLED": "false",
        "BROADCAST_ENABLED": "false",
        "REDIS_URL": "redis://127.0.0.1:1/0",
        "LEDGER_DIR": str(tmp_path / "ledgers"),
        "LEDGER_PORT": "1",
        "AGENTIHOOKS_SWARM": SLUG,
        "AGENTIHOOKS_AGENT_NAME": ME,
        "AGENTIHOOKS_SWARM_TASK": "t1",
        "AGENTIHOOKS_GATE_SUBAGENTS": "enforce",
    }

    def run(tool, tool_input, extra=None):
        payload = {
            "hook_event_name": "PreToolUse",
            "session_id": SID,
            "permission_mode": "bypassPermissions",
            "cwd": str(tmp_path),
            "tool_name": tool,
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

    def rows():
        path = tmp_path / ".agentihooks" / "swarm" / SLUG / "gates" / "log.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    run.rows = rows
    return run


def launch(kind):
    return {"subagent_type": kind, "description": "read the diff", "prompt": "review"}


def rerun(command):
    return {"command": command, "description": "rerun"}


def test_a_third_launch_is_refused_whatever_the_sub_agents_name(hook):
    assert hook("Agent", launch("standards-reader")).returncode == 0
    assert hook("Task", launch("Explore")).returncode == 0
    denied = hook("Agent", launch("a-new-name"))
    assert denied.returncode == 2, denied.stdout + denied.stderr
    assert "the sub-agent budget for task t1 is spent: 2 of 2 launches" in denied.stderr
    assert [(r["gate"], r["kind"], r["tool"]) for r in hook.rows()] == [
        ("subagents", "count", "Agent"),
        ("subagents", "count", "Task"),
        ("subagents", "deny", "Agent"),
    ]


def test_a_third_continuation_is_refused_and_a_second_allowed(hook):
    assert [hook("SendMessage", {"to": "reader", "message": "again"}).returncode for _ in range(3)] == [0, 0, 2]


def test_the_sub_agent_counter_ships_in_observe(hook):
    observe = {"AGENTIHOOKS_GATE_SUBAGENTS": ""}
    assert [hook("Agent", launch("x"), observe).returncode for _ in range(3)] == [0, 0, 0]
    assert [r["kind"] for r in hook.rows()] == ["count", "count", "observe"]


def test_a_third_rerun_of_a_failed_job_is_refused(hook):
    assert hook("Bash", rerun("gh run rerun 1 --failed")).returncode == 0
    assert hook("Bash", rerun("gh run rerun --job 5")).returncode == 0
    denied = hook("Bash", rerun("gh run rerun 2 --failed"))
    assert denied.returncode == 2, denied.stdout + denied.stderr
    assert f"CI reruns on pull request head {HEAD[:12]} are spent: 2 of 2" in denied.stderr
    assert [(r["gate"], r["kind"]) for r in hook.rows()] == [
        ("reruns", "count"),
        ("reruns", "count"),
        ("reruns", "deny"),
    ]


def test_a_rerun_of_a_job_cancelled_without_a_runner_is_allowed_past_the_cap(hook):
    for run in ("1", "2"):
        assert hook("Bash", rerun(f"gh run rerun {run}")).returncode == 0
    allowed = hook("Bash", rerun("gh run rerun 9"))
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    assert [r["kind"] for r in hook.rows()] == ["count", "count"]
