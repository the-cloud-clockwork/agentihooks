"""The claim Stop gate through the real hook executor, a real ledger server and a Redis server, each its own process:
a stop after the merge is blocked, a stop on pending checks passes with a recorded wait, the third block blocks the task.
"""

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

SLUG, ME, SID = "stopexec-2026-01-01", "engineer@abcdef-0001", "sid-stop"
URL = "https://github.com/o/r/pull/7"
PACKAGE = ".".join(("scripts", "gates"))
SHIM = f"import runpy\nimport sys\n\nsys.argv = ['gate', 'claim-stop']\nrunpy.run_module({PACKAGE!r}, run_name='__main__')\n"
GH = '#!/bin/sh\n[ -f "$FAKE_GH_ANSWER" ] || exit 1\ncat "$FAKE_GH_ANSWER"\n'
PENDING = {"data": {"resource": {"state": "OPEN", "headRefOid": "first", "commits": {"nodes": []}}}}
MERGED = {
    "data": {
        "resource": {
            "state": "MERGED",
            "mergedAt": "2026-01-01T00:00:00+00:00",
            "headRefOid": "first",
            "commits": {"nodes": []},
        }
    }
}

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def rig(tmp_path, monkeypatch, ledger_port):
    import redis
    from fakeredis import TcpFakeServer

    server = TcpFakeServer(("127.0.0.1", 0), server_type="redis")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"redis://127.0.0.1:{server.server_address[1]}/0?protocol=3"
    client = redis.Redis.from_url(url, decode_responses=True)
    ledgers = tmp_path / "ledgers"
    ledgers.mkdir()
    monkeypatch.setattr(core, "LEDGER_DIR", ledgers)
    content = {
        "title": "Demo",
        "overview": "o",
        "sources": [],
        "phases": [{"title": "one", "description": "d"}],
        "tasks": [{"title": "Build", "description": "d", "phase": "p1", "lane": "eng"}],
    }
    doc = new_ledger.build_doc(content)
    doc["tasks"][0].update(state="claimed", claimed_by=ME)
    html_path, _ = core.paths(SLUG)
    html_path.write_text(legacy_page.render(doc, SLUG, 8765), encoding="utf-8")
    core.sync(SLUG)
    task = doc["tasks"][0]["id"]
    home, bundle, bin_dir = tmp_path / "ahome", tmp_path / "bundle", tmp_path / "bin"
    conditions = bundle / ".claude" / "conditions"
    for folder in (conditions, home, bin_dir):
        folder.mkdir(parents=True)
    (home / "state.json").write_text(json.dumps({"bundle": {"path": str(bundle)}}))
    (conditions / "stop-claimstop.py").write_text(SHIM)
    (bin_dir / "gh").write_text(GH)
    (bin_dir / "gh").chmod(0o755)
    answer = tmp_path / "gh-answer.json"
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "PYTHONPATH": str(ROOT),
        "AGENTIHOOKS_HOME": str(home),
        "AGENTIHOOKS_TARGET": "claude",
        "AGENTIHOOKS_DISABLE_BYPASS_LOOKUP": "1",
        "CONDITIONS_ENABLED": "true",
        "BRAIN_ENABLED": "false",
        "BROADCAST_ENABLED": "false",
        "REDIS_URL": url,
        "AGENTIHOOKS_SWARM_REDIS_URL": url,
        "LEDGER_DIR": str(ledgers),
        "LEDGER_PORT": str(ledger_port),
        "LEDGER_AUTOSTART": "1",
        "AGENTIHOOKS_SWARM": SLUG,
        "AGENTIHOOKS_AGENT_NAME": ME,
        "AGENTIHOOKS_SWARM_LANE": "eng",
        "AGENTIHOOKS_SWARM_TASK": task,
        "FAKE_GH_ANSWER": str(answer),
    }
    client.hset(
        f"{KEY_ROOT}:swarm:{SLUG}:config",
        mapping={"slug": SLUG, "repo": "/repo", "max_eng": 1, "max_ci": 0, "state": "running", "gates": "{}"},
    )

    def stop(extra=None, via=()):
        payload = {"hook_event_name": "Stop", "session_id": SID, "cwd": str(tmp_path), "stop_hook_active": False}
        return subprocess.run(
            [*via, sys.executable, "-m", "hooks"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=ROOT,
            env={**env, **(extra or {})},
            timeout=90,
        )

    def ledger_task():
        from scripts.swarm_ledger.repository import repository

        return next(t for t in repository.get_document(SLUG)["tasks"] if t["id"] == task)

    def cli(*argv):
        done = subprocess.run(
            [sys.executable, str(SCRIPTS / "ledger.py"), "--slug", SLUG, "--as", ME, *argv],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        assert done.returncode == 0, done.stderr

    def set_task(**fields):
        cli("task", "set", task, *(f"{key}={value}" for key, value in fields.items()))

    def rows():
        path = tmp_path / ".agentihooks" / "swarm" / SLUG / "gates" / "log.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    rig = type("Rig", (), {})()
    rig.stop, rig.redis, rig.answer, rig.task, rig.ledger_task, rig.rows = stop, client, answer, task, ledger_task, rows
    rig.set_task, rig.home = set_task, tmp_path
    try:
        cli("join")
        yield rig
    finally:
        subprocess.run([sys.executable, str(SCRIPTS / "ledger_server.py"), "--stop"], env=env, timeout=30)
        server.shutdown()
        server.server_close()


def test_a_stop_with_work_owed_is_blocked_and_the_third_blocks_the_task(rig):
    first = rig.stop()
    assert first.returncode == 2, (first.stderr, [r["reason"] for r in rig.rows()])
    assert f"you hold task {rig.task} with no open pull request and no wait" in first.stderr
    assert "(stop block 1 of 2; the next one blocks the task)" in first.stderr
    assert rig.stop().returncode == 2
    third = rig.stop()
    assert third.returncode == 0, third.stderr
    assert rig.rows()[-1]["kind"] == "blocked", rig.rows()[-1]["reason"]
    assert rig.ledger_task()["state"] == "blocked"
    assert [(r["gate"], r["kind"], r["agent"]) for r in rig.rows()] == [
        ("claim-stop", "deny", ME),
        ("claim-stop", "deny", ME),
        ("claim-stop", "blocked", ME),
    ]


def test_a_stop_after_the_merge_is_blocked_and_one_on_pending_checks_passes_with_a_wait(rig):
    rig.set_task(state="pr", pr_url=URL)
    rig.answer.write_text(json.dumps(MERGED))
    merged = rig.stop()
    assert merged.returncode == 2
    assert f"your pull request {URL} merged: close the task now" in merged.stderr
    rig.answer.write_text(json.dumps(PENDING))
    pending = rig.stop()
    assert pending.returncode == 0, pending.stderr
    held = json.loads(rig.redis.get(f"{KEY_ROOT}:swarm:{SLUG}:wait:{ME}"))
    assert held["on"] == {"kind": "checks", "target": URL}


def test_a_tune_task_with_a_merged_fix_stops_on_its_measurement_wait_and_owes_its_contract_without_it(rig):
    rig.set_task(state="pr", pr_url=URL, kind="tune")
    rig.answer.write_text(json.dumps(MERGED))
    bare = rig.stop()
    assert bare.returncode == 2
    assert f"your pull request {URL} merged, and tune task {rig.task} closes on its proof contract" in bare.stderr
    wait = {"until": 2**53, "reason": "after measurement", "at": 1}
    rig.redis.set(f"{KEY_ROOT}:swarm:{SLUG}:wait:{ME}", json.dumps(wait))
    waiting = rig.stop()
    assert waiting.returncode == 0, waiting.stderr


def test_observe_mode_lets_the_stop_through_and_logs_it(rig):
    rig.answer.write_text(json.dumps(MERGED))
    rig.set_task(state="pr", pr_url=URL)
    rig.redis.hset(f"{KEY_ROOT}:swarm:{SLUG}:config", "gates", json.dumps({"claim-stop": "observe"}))
    passed = rig.stop({"AGENTIHOOKS_GATE_CLAIM_STOP": "enforce"})
    assert passed.returncode == 0, passed.stderr
    assert [(r["gate"], r["kind"]) for r in rig.rows()] == [("claim-stop", "observe")]


def _wrappers(folder):
    folder.mkdir()
    bodies = {"launcher": "export AGENTIHOOKS_SWARM_LAUNCHER=$$\n", "claude": "", "codex": ""}
    for name, body in bodies.items():
        (folder / name).write_text(f'#!/bin/bash\n{body}"$@"\nexit $?\n')
        (folder / name).chmod(0o755)
    return [str(folder / name) for name in bodies]


def test_a_child_session_started_inside_the_agent_leaves_its_task_and_record_alone(rig):
    from scripts.swarm.store import AgentRecord, RedisStore

    store = RedisStore(rig.redis)
    store.put_agent(SLUG, AgentRecord(ME, "eng", rig.task, harness="claude"))
    record = rig.redis.hget(f"{KEY_ROOT}:swarm:{SLUG}:agents", ME)
    rig.set_task(state="pr", pr_url=URL)
    rig.answer.write_text(json.dumps(MERGED))
    launcher, claude, codex = _wrappers(rig.home / "procs")
    for _ in range(3):
        child = rig.stop(via=[launcher, claude, "/bin/bash", "-c", '"$@"; exit $?', "bash", codex])
        assert child.returncode == 0, child.stderr
    assert (rig.ledger_task()["state"], rig.ledger_task()["claimed_by"]) == ("pr", ME)
    assert rig.redis.hget(f"{KEY_ROOT}:swarm:{SLUG}:agents", ME) == record
    assert rig.rows() == []
    parent = rig.stop(via=[launcher, claude])
    assert parent.returncode == 2
    assert f"your pull request {URL} merged: close the task now" in parent.stderr
