"""The build gate through the real hook executor: a doghouse plan whose generator piece was cut refuses an edit in the
generator's area, allows the light's, and refuses every edit before the plan is traced."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.swarm import trace_plan

ROOT = Path(__file__).resolve().parents[2]
ME, SLUG, TASK, SID = "engineer@100001-0001", "demo", "t1", "sid-build"
MATCHER = "edit+write+bash.git"
PACKAGE = ".".join(("scripts", "gates"))
SHIM = f"import runpy\nimport sys\n\nsys.argv = ['gate', 'build']\nrunpy.run_module({PACKAGE!r}, run_name='__main__')\n"
DOGHOUSE = (
    "- walls and roof | doghouse/frame, doghouse/roof | the house needs a shell\n"
    "- a light over the door | doghouse/light | the dog sleeps there at night\n"
    "- a diesel generator | power/generator | it powers the light\n"
)


@pytest.fixture
def hook(tmp_path):
    home, bundle, repo = tmp_path / "ahome", tmp_path / "bundle", tmp_path / "repo"
    conditions = bundle / ".claude" / "conditions"
    conditions.mkdir(parents=True)
    home.mkdir()
    (repo / ".git").mkdir(parents=True)
    (home / "state.json").write_text(json.dumps({"bundle": {"path": str(bundle)}}))
    (conditions / f"pre-{MATCHER}-build.py").write_text(SHIM)
    folder = tmp_path / ".agentihooks" / "swarm" / SLUG / "tasks" / TASK
    folder.mkdir(parents=True)
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
        "AGENTIHOOKS_GATE_BUILD": "enforce",
    }

    def run(rel, extra=None):
        payload = {
            "hook_event_name": "PreToolUse",
            "session_id": SID,
            "permission_mode": "bypassPermissions",
            "cwd": str(repo),
            "tool_name": "Write",
            "tool_input": {"file_path": str(repo / rel), "content": "x\n"},
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

    def traced():
        (folder / trace_plan.PLAN).write_text(DOGHOUSE)
        pieces = trace_plan.parse(DOGHOUSE, TASK)
        rows = [
            {"what": p.what, "areas": list(p.areas), "why": p.why, "probability": prob, "kept": prob >= 0.3}
            for p, prob in zip(pieces, (0.95, 0.9, 0.1), strict=True)
        ]
        record = {"verdict": "pass", "plan_hash": trace_plan.plan_hash(pieces), "pieces": rows, "reasons": []}
        (folder / trace_plan.VERDICT).write_text(json.dumps(record))

    def rows():
        path = tmp_path / ".agentihooks" / "swarm" / SLUG / "gates" / "log.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    run.traced, run.rows = traced, rows
    return run


def test_an_edit_before_the_plan_is_traced_is_denied(hook):
    denied = hook("doghouse/light/lamp.py")
    assert denied.returncode == 2, denied.stdout + denied.stderr
    assert f"agentihooks swarm {SLUG} trace-plan" in denied.stderr


def test_the_cut_generator_area_is_denied_and_the_light_area_allowed(hook):
    hook.traced()
    allowed = hook("doghouse/light/lamp.py")
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    denied = hook("power/generator/diesel.py")
    assert denied.returncode == 2, denied.stdout + denied.stderr
    assert "build gate: outside your traced plan: power/generator/diesel.py." in denied.stderr
    assert [(r["gate"], r["kind"], r["agent"], r["task"]) for r in hook.rows()] == [("build", "deny", ME, TASK)]


def test_observe_mode_lets_the_edit_through_and_logs_it(hook):
    hook.traced()
    assert hook("power/generator/diesel.py", extra={"AGENTIHOOKS_GATE_BUILD": "observe"}).returncode == 0
    assert [r["kind"] for r in hook.rows()] == ["observe"]
