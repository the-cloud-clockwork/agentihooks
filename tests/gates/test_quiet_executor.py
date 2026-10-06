"""The quiet claim gate through the real hook executor and a bash flag shim: a quiet agent's Bash call is refused and
its ledger command allowed, after progress Bash is allowed again, and an agent waiting on a task is never flagged."""

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.gates.verdicts import Verdicts
from scripts.swarm import idle
from scripts.swarm.store import AgentRecord, RedisStore

pytestmark = pytest.mark.xdist_group("fakeredis")

ROOT = Path(__file__).resolve().parents[2]
ME, SLUG, SID = "engineer@100001-0001", "demo", "sid-quiet"
MIN = 60_000
NOW = 100 * MIN
PACKAGE = ".".join(("scripts", "gates"))
quiet = importlib.import_module(f"{PACKAGE}.quiet")
SHIM = (
    "#!/usr/bin/env bash\n"
    '[ -e "$HOME/.agentihooks/swarm/$AGENTIHOOKS_SWARM/gates/quiet/$AGENTIHOOKS_AGENT_NAME" ] || exit 0\n'
    f"exec {sys.executable} -m {PACKAGE} quiet\n"
)
LS = {"command": "ls", "description": "probe"}
LEDGER = {"command": f"agentihooks ledger --slug {SLUG} --as {ME} comment tasks/t1 'on it'", "description": "probe"}


class FakeLedger:
    def __init__(self):
        self.comments = []

    def comment(self, slug, task_id, text, by):
        self.comments.append((task_id, text, by))


@pytest.fixture
def hook(tmp_path, monkeypatch):
    import fakeredis

    home, bundle = tmp_path / "ahome", tmp_path / "bundle"
    conditions = bundle / ".claude" / "conditions"
    conditions.mkdir(parents=True)
    home.mkdir()
    (home / "state.json").write_text(json.dumps({"bundle": {"path": str(bundle)}}))
    (conditions / "pre-any-quiet.sh").write_text(SHIM)
    monkeypatch.setenv("HOME", str(tmp_path))
    swarm_home = tmp_path / ".agentihooks" / "swarm"
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.put_agent(SLUG, AgentRecord(name=ME, lane="eng", task="t1", started_at=NOW - 40 * MIN))
    rows = {"t1": {"id": "t1", "state": "claimed", "claimed_by": ME}}
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

    def run(tool="Bash", tool_input=LS):
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
            env=env,
            timeout=60,
        )

    def rows_logged():
        path = swarm_home / SLUG / "gates" / "log.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    run.store, run.rows, run.home, run.logged = store, rows, swarm_home, rows_logged
    run.tick = lambda now=NOW: quiet.quiet_pass(store, SLUG, rows, now, swarm_home)
    return run


def test_a_quiet_agent_is_refused_until_it_reports_progress(hook):
    assert hook().returncode == 0, "no flag: the shim's file test lets the call through"
    assert hook.tick() == [f"raised the quiet flag on {ME}: 40 minutes with no progress on task t1"]
    denied = hook()
    assert denied.returncode == 2, denied.stdout + denied.stderr
    assert "quiet for 40 minutes on task t1: run agentihooks swarm demo progress --doing" in denied.stderr
    assert hook(tool="Read", tool_input={"file_path": "/etc/hostname"}).returncode == 2
    assert hook(tool_input=LEDGER).returncode == 0, "the ledger command passes"
    assert [(r["gate"], r["kind"]) for r in hook.logged()] == [("quiet", "count"), ("quiet", "deny"), ("quiet", "deny")]
    agent = hook.store.agents(SLUG)[0]
    quiet.report(hook.store, FakeLedger(), SLUG, agent, "writing tests", "they pass", NOW, hook.home)
    assert hook().returncode == 0
    assert hook.tick(NOW + MIN) == []


def test_an_agent_waiting_on_a_task_is_never_flagged(hook):
    on = {"kind": "task", "target": "t2"}
    idle.declare_wait(hook.store.redis, SLUG, ME, NOW + 60 * MIN, "t2 first", NOW - MIN, on=on)
    assert hook.tick() == []
    assert Verdicts(SLUG, quiet.NAME, hook.home).read(ME) is None
    assert hook().returncode == 0
