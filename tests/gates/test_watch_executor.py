"""The watch budget through the real hook executor: the 21st watch with no action is denied, an action frees it, and a
ledger watch re-arm with no live watcher passes."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ME, SLUG, SID = "engineer@100001-0001", "demo", "sid-watch"
MATCHER = "monitor+taskoutput+bashoutput+bash.gh+bash.sleep+bash.agentihooks"
PACKAGE = ".".join(("scripts", "gates"))
SHIM = f"import runpy\nimport sys\n\nsys.argv = ['gate', 'watch']\nrunpy.run_module({PACKAGE!r}, run_name='__main__')\n"
CHECKS = {"command": "gh pr checks 12", "description": "probe"}
REARM = {"command": f"agentihooks ledger watch {SLUG} --as {ME}", "description": "ledger events"}
ACT = {"command": "git commit -m probe", "description": "probe"}


@pytest.fixture
def hook(tmp_path):
    home, bundle = tmp_path / "ahome", tmp_path / "bundle"
    conditions = bundle / ".claude" / "conditions"
    conditions.mkdir(parents=True)
    home.mkdir()
    (home / "state.json").write_text(json.dumps({"bundle": {"path": str(bundle)}}))
    (conditions / f"pre-{MATCHER}-watch.py").write_text(SHIM)
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
        "AGENTIHOOKS_SWARM_TASK": "t1",
    }

    def run(tool="Bash", tool_input=CHECKS, extra=None):
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

    recorded = home / "swarm-activity" / SLUG / f"{ME}.jsonl"

    def seed(count):
        recorded.parent.mkdir(parents=True, exist_ok=True)
        with recorded.open("a") as out:
            out.writelines(json.dumps({"kind": "watch", "at": 0}) + "\n" for _ in range(count))

    def kinds():
        return [json.loads(line)["kind"] for line in recorded.read_text().splitlines()]

    def revived():
        return [bool(json.loads(line).get("revived")) for line in recorded.read_text().splitlines()]

    def rows():
        path = tmp_path / ".agentihooks" / "swarm" / SLUG / "gates" / "log.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def beat():
        path = tmp_path / "ledgers" / ".sessions" / f"{SLUG}.{ME}.watch"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    run.seed, run.rows, run.beat, run.kinds, run.revived = seed, rows, beat, kinds, revived
    return run


def test_the_twenty_first_watch_with_no_action_is_denied_and_an_action_frees_it(hook):
    hook.seed(19)
    assert hook().returncode == 0
    assert hook.kinds() == ["watch"] * 20
    denied = hook()
    assert denied.returncode == 2, denied.stdout + denied.stderr
    assert "watch budget: 20 watch calls since your last action (limit 20)" in denied.stderr
    assert f"agentihooks swarm {SLUG} wait <minutes>" in denied.stderr
    assert hook.kinds() == ["watch"] * 20, "a denied call is not counted"
    assert [(r["gate"], r["kind"], r["agent"], r["task"]) for r in hook.rows()] == [("watch", "deny", ME, "t1")]
    assert hook(tool_input=ACT).returncode == 0
    assert hook.kinds()[-1] == "act"
    assert hook().returncode == 0


def test_a_rearm_with_no_live_watcher_passes_and_one_with_a_live_watcher_is_denied(hook):
    hook.seed(20)
    assert hook(tool="Monitor", tool_input=REARM).returncode == 0
    hook.beat()
    assert hook(tool="Monitor", tool_input=REARM).returncode == 2


def test_a_rearm_let_through_for_a_dead_watcher_is_recorded_as_revived_and_not_counted(hook):
    hook.seed(20)
    assert hook(tool="Monitor", tool_input=REARM).returncode == 0
    assert hook.kinds() == ["watch"] * 21
    assert hook.revived() == [False] * 20 + [True]
    denied = hook()
    assert denied.returncode == 2, denied.stdout + denied.stderr
    assert "watch budget: 20 watch calls since your last action (limit 20)" in denied.stderr


def test_observe_mode_lets_the_watch_through_and_logs_it(hook):
    hook.seed(20)
    assert hook(extra={"AGENTIHOOKS_GATE_WATCH": "observe"}).returncode == 0
    assert [r["kind"] for r in hook.rows()] == ["observe"]
