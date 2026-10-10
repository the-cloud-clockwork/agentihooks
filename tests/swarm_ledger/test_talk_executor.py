"""The talk budget through the real ledger server, the real ledger hook and the ledger CLI, each its own process."""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from scripts.swarm.keyspace import ROOT as KEY_ROOT

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402
import new_ledger  # noqa: E402

from tests.swarm_ledger import legacy_page  # noqa: E402

SLUG, ENG, SID = "talkexec-2026-01-01", "engineer@abcdef-0001", "sid-talk"
BUDGET = 10

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def rig(tmp_path, monkeypatch, ledger_port):
    import redis
    from fakeredis import TcpFakeServer

    server = TcpFakeServer(("127.0.0.1", 0), server_type="redis")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"redis://127.0.0.1:{server.server_address[1]}/0"
    client = redis.Redis.from_url(url, decode_responses=True)
    client.hset(
        f"{KEY_ROOT}:swarm:{SLUG}:config",
        mapping={"slug": SLUG, "repo": "/repo", "max_eng": 1, "max_ci": 0, "state": "running", "gates": "{}"},
    )
    ledgers = tmp_path / "ledgers"
    ledgers.mkdir()
    monkeypatch.setattr(core, "LEDGER_DIR", ledgers)
    content = {"title": "Demo", "overview": "o", "sources": [], "phases": [{"title": "one", "description": "d"}]}
    html_path, _ = core.paths(SLUG)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    core.sync(SLUG)
    (tmp_path / "home" / ".agentihooks").mkdir(parents=True)
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "PYTHONPATH": str(ROOT),
        "LEDGER_DIR": str(ledgers),
        "LEDGER_PORT": str(ledger_port),
        "LEDGER_AUTOSTART": "1",
        "AGENTIHOOKS_SWARM_REDIS_URL": url,
        "AGENTIHOOKS_SWARM": SLUG,
        "AGENTIHOOKS_AGENT_NAME": ENG,
    }

    def cli(*argv):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "ledger.py"), "--slug", SLUG, "--as", ENG, *argv],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    def hook(command, stdout=""):
        payload = {
            "session_id": SID,
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "tool_response": {"stdout": stdout},
        }
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "ledger_hook.py")],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    def mode(chosen):
        client.hset(f"{KEY_ROOT}:swarm:{SLUG}:config", "gates", json.dumps({"talk": chosen}))

    def rows():
        path = tmp_path / "home" / ".agentihooks" / "swarm" / SLUG / "gates" / "log.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    rig = type("Rig", (), {})()
    rig.cli, rig.hook, rig.mode, rig.rows, rig.redis = cli, hook, mode, rows, client
    try:
        joined = cli("join")
        assert joined.returncode == 0, joined.stderr
        bound = hook(f"agentihooks ledger --slug {SLUG} --as {ENG} join", joined.stdout)
        assert bound.returncode == 0, bound.stderr
        yield rig
    finally:
        subprocess.run([sys.executable, str(SCRIPTS / "ledger_server.py"), "--stop"], env=env, timeout=30)
        server.shutdown()
        server.server_close()


def say_budget(rig):
    from scripts.gates.progress import Progress

    Progress(rig.redis, SLUG).outcome(ENG, "pushed", 1)
    for n in range(BUDGET):
        said = rig.cli("say", f"step {n} landed")
        assert said.returncode == 0, said.stderr


def test_the_write_past_the_budget_is_refused_until_a_push_lands(rig):
    rig.mode("enforce")
    say_budget(rig)
    refused = rig.cli("say", "one more line")
    assert refused.returncode != 0
    assert f"talk refused: {ENG} made {BUDGET} talk writes since its last outcome" in refused.stderr
    assert [(r["gate"], r["kind"], r["agent"]) for r in rig.rows()] == [("talk", "deny", ENG)]
    pushed = rig.hook("git push -u origin feature")
    assert pushed.returncode == 0, pushed.stderr
    assert rig.redis.hgetall(f"{KEY_ROOT}:swarm:{SLUG}:progress:{ENG}").get("outcome") == "pushed"
    passed = rig.cli("say", "one more line")
    assert passed.returncode == 0, passed.stderr
    assert json.loads(passed.stdout) == {"posted": True}


def test_observe_posts_the_line_and_logs_the_would_be_deny(rig):
    rig.mode("observe")
    say_budget(rig)
    passed = rig.cli("say", "one more line")
    assert passed.returncode == 0, passed.stderr
    assert [(r["gate"], r["kind"]) for r in rig.rows()] == [("talk", "observe")]
