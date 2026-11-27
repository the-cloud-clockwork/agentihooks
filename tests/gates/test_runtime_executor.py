"""The gate runtime through the real hook executor: observe mode, counted fail-open and the operator's typed lift."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

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

    def run(
        event: str, extra: dict[str, str] | None = None, gate: str = "identity", shim: str = SHIM, **fields: object
    ) -> subprocess.CompletedProcess[str]:
        (conditions / f"pre-bash-{gate}.py").write_text(shim)
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


@pytest.mark.feature("gate-import-failure-alerts")
def test_a_shim_that_cannot_import_the_gates_lets_the_call_through_and_its_hook_reports_it(
    hook: Any, tmp_path: Path
) -> None:
    broken = tmp_path / "broken" / "scripts"
    (broken / "gates").mkdir(parents=True)
    (broken / "__init__.py").write_text("")
    (broken / "gates" / "__init__.py").write_text("import a_dependency_lost_with_the_worktree\n")
    shim = (
        f"import sys\nsys.path.insert(0, {str(broken.parent)!r})\n"
        f'from {PACKAGE}.entry import main\n\nraise SystemExit(main(["reruns"]))\n'
    )
    proc = hook("PreToolUse", gate="rerun_budget", shim=shim)
    assert proc.returncode == 0, proc.stderr
    decided = json.loads(proc.stdout).get("hookSpecificOutput", {}) if proc.stdout.strip() else {}
    assert decided.get("permissionDecision") != "deny" and "updatedInput" not in decided
    rows = hook.rows()
    assert kinds(rows) == [("reruns", "fail-open", ME, "t1", "Bash")]
    reason = rows[0]["reason"]
    assert "No module named 'a_dependency_lost_with_the_worktree'" in reason and sys.executable in reason


@pytest.mark.feature("gate-import-failure-alerts")
def test_a_gate_module_the_entry_cannot_import_is_reported_once_by_the_child(hook: Any) -> None:
    shim = (
        "import importlib\n\n"
        f"entry = importlib.import_module({PACKAGE + '.entry'!r})\n"
        f"entry.GATES['absent'] = (lambda: importlib.import_module({PACKAGE + '.absent_gate_module'!r}),)\n"
        "raise SystemExit(entry.main(['absent']))\n"
    )
    proc = hook("PreToolUse", gate="absent", shim=shim)
    assert proc.returncode == 0, proc.stderr
    rows = hook.rows()
    assert kinds(rows) == [("absent", "fail-open", ME, "t1", "Bash")]
    assert f"No module named '{PACKAGE}.absent_gate_module'" in rows[0]["reason"]
