"""The gate runtime through the real hook executor: observe mode, counted fail-open and the operator's typed lift."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ME, OTHER, SLUG, SID = "engineer@1-1", "engineer@1-2", "demo", "sid-runtime"
PACKAGE = ".".join(("scripts", "gates"))
SHIM = (
    "import importlib\nimport sys\n\n"
    f"entry = importlib.import_module({PACKAGE + '.entry'!r})\n"
    "class Raising:\n"
    "    name = 'raising'\n"
    "    default_mode = 'enforce'\n"
    "    def matches(self, call):\n"
    "        return call.tool == 'Bash'\n"
    "    def decide(self, call, who, state):\n"
    "        raise RuntimeError('planted fault')\n"
    "entry.GATES['raising'] = Raising()\n"
    "raise SystemExit(entry.main([sys.argv[0].rsplit('-', 1)[-1][:-3]]))\n"
)
DENY = f"agentihooks ledger --slug {SLUG} --as {OTHER} say hi"


@pytest.fixture
def hook(tmp_path):
    home, bundle = tmp_path / "ahome", tmp_path / "bundle"
    conditions = bundle / ".claude" / "conditions"
    conditions.mkdir(parents=True)
    home.mkdir()
    (home / "state.json").write_text(json.dumps({"bundle": {"path": str(bundle)}}))
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
        "LEDGER_DIR": str(tmp_path / "ledgers"),
        "LEDGER_PORT": "1",
        "AGENTIHOOKS_SWARM": SLUG,
        "AGENTIHOOKS_AGENT_NAME": ME,
        "AGENTIHOOKS_SWARM_TASK": "t1",
    }

    def run(event, extra=None, gate="identity", **fields):
        (conditions / f"pre-bash-{gate}.py").write_text(SHIM)
        payload = {
            "hook_event_name": event,
            "session_id": SID,
            "permission_mode": "bypassPermissions",
            "cwd": str(tmp_path),
            **fields,
        }
        if event == "PreToolUse":
            payload.update(tool_name="Bash", tool_input={"command": DENY, "description": "probe"})
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


def kinds(rows):
    return [(r["gate"], r["kind"], r["agent"], r["task"], r["tool"]) for r in rows]


def test_enforce_denies_and_writes_a_deny_row(hook):
    proc = hook("PreToolUse")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert f"cannot act as {OTHER}" in proc.stderr
    assert kinds(hook.rows()) == [("identity", "deny", ME, "t1", "Bash")]


def test_observe_lets_the_call_through_and_writes_an_observe_row(hook):
    proc = hook("PreToolUse", {"AGENTIHOOKS_GATE_IDENTITY": "observe"})
    assert proc.returncode == 0, proc.stderr
    assert "cannot act as" not in proc.stdout + proc.stderr
    assert kinds(hook.rows()) == [("identity", "observe", ME, "t1", "Bash")]
    assert f"cannot act as {OTHER}" in hook.rows()[0]["reason"]


def test_off_skips_the_gate_and_writes_nothing(hook):
    assert hook("PreToolUse", {"AGENTIHOOKS_GATE_IDENTITY": "off"}).returncode == 0
    assert hook.rows() == []


def test_a_raising_gate_fails_open_and_is_counted(hook):
    proc = hook("PreToolUse", gate="raising")
    assert proc.returncode == 0, proc.stderr
    assert kinds(hook.rows()) == [("raising", "fail-open", ME, "t1", "Bash")]
    assert hook.rows()[0]["reason"] == "RuntimeError: planted fault"


def test_the_operators_typed_lift_lets_the_next_deny_through(hook):
    assert hook("UserPromptSubmit", prompt="lift the identity gate").returncode == 0
    assert hook.rows() == [], "the opening prompt of a swarm session is not the operator's words"
    assert hook("PreToolUse").returncode == 2
    assert hook("UserPromptSubmit", prompt="please lift the identity gate").returncode == 0
    proc = hook("PreToolUse")
    assert proc.returncode == 0, proc.stderr
    assert kinds(hook.rows()) == [
        ("identity", "deny", ME, "t1", "Bash"),
        ("identity", "lift", ME, "t1", ""),
        ("identity", "observe", ME, "t1", "Bash"),
    ]
    assert hook.rows()[2]["reason"].startswith("lifted by the operator: ")


def test_a_lift_of_an_unknown_gate_arms_nothing(hook):
    hook("UserPromptSubmit", prompt="opening")
    hook("UserPromptSubmit", prompt="lift the bogus gate")
    assert hook("PreToolUse").returncode == 2
    assert [r["kind"] for r in hook.rows()] == ["deny"]
