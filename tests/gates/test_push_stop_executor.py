"""The push Stop gate through the real hook executor, a real ledger server, a Redis server and a real git origin:
unpushed commits reach origin and are recorded on the task, dirty work is refused with the template, a clean pushed
worktree with its pull request stops freely.
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

SLUG, ME, SID = "pushexec-2026-01-01", "engineer@abcdef-0001", "sid-push"
BRANCH = "engineer-abcdef-0001"
URL = "https://github.com/o/r/pull/7"
PACKAGE = ".".join(("scripts", "gates"))
SHIM = f"import runpy\nimport sys\n\nsys.argv = ['gate', 'push-stop']\nrunpy.run_module({PACKAGE!r}, run_name='__main__')\n"
TEMPLATE = (
    "You cannot leave uncommitted or unpushed changes. Commit them now. If the task is done, open the pull request. "
    "If not, update the issue and the pull request and record progress on the ledger."
)
IDENTITY = ("-c", "user.name=t", "-c", "user.email=t@example.com")

pytestmark = pytest.mark.xdist_group("fakeredis")


def git(path, *args):
    done = subprocess.run(["git", "-C", str(path), *IDENTITY, *args], capture_output=True, text=True, check=True)
    return done.stdout.strip()


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
    html_path.write_text(new_ledger.render(doc, SLUG, 8765), encoding="utf-8")
    core.sync(SLUG)
    task = doc["tasks"][0]["id"]
    home, bundle = tmp_path / "ahome", tmp_path / "bundle"
    conditions = bundle / ".claude" / "conditions"
    for folder in (conditions, home):
        folder.mkdir(parents=True)
    (home / "state.json").write_text(json.dumps({"bundle": {"path": str(bundle)}}))
    (conditions / "stop-pushstop.py").write_text(SHIM)
    origin, seed, trees = tmp_path / "origin.git", tmp_path / "seed", tmp_path / "worktrees"
    git(tmp_path, "init", "--bare", "-b", "dev", str(origin))
    git(tmp_path, "clone", str(origin), str(seed))
    (seed / "readme").write_text("seed\n")
    git(seed, "add", "readme")
    git(seed, "commit", "-m", "seed")
    git(seed, "push", "origin", "HEAD:refs/heads/dev")
    tree = trees / "repo" / BRANCH
    git(seed, "fetch", "origin")
    git(seed, "worktree", "add", "-b", BRANCH, str(tree), "origin/dev")
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
        "REDIS_URL": url,
        "AGENTIHOOKS_SWARM_REDIS_URL": url,
        "LEDGER_DIR": str(ledgers),
        "LEDGER_PORT": str(ledger_port),
        "LEDGER_AUTOSTART": "1",
        "WORKTREE_ROOT": str(trees),
        "AGENTIHOOKS_SWARM": SLUG,
        "AGENTIHOOKS_AGENT_NAME": ME,
        "AGENTIHOOKS_SWARM_LANE": "eng",
        "AGENTIHOOKS_SWARM_TASK": task,
    }
    client.hset(
        f"{KEY_ROOT}:swarm:{SLUG}:config",
        mapping={"slug": SLUG, "repo": "/repo", "max_eng": 1, "max_ci": 0, "state": "running", "gates": "{}"},
    )

    def stop():
        payload = {"hook_event_name": "Stop", "session_id": SID, "cwd": str(tmp_path), "stop_hook_active": False}
        return subprocess.run(
            [sys.executable, "-m", "hooks"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=ROOT,
            env=env,
            timeout=90,
        )

    def ledger_task():
        return next(t for t in json.loads((ledgers / f"{SLUG}.json").read_text())["tasks"] if t["id"] == task)

    def cli(*argv):
        done = subprocess.run(
            [sys.executable, str(SCRIPTS / "ledger.py"), "--slug", SLUG, "--as", ME, *argv],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        if done.returncode:
            log = ledgers / ".server.log"
            pytest.fail(f"{done.stderr}\nLedger server output:\n{log.read_text() if log.exists() else 'no server log'}")

    def commit(name="work"):
        (tree / name).write_text(name)
        git(tree, "add", name)
        git(tree, "commit", "-m", name)
        return git(tree, "rev-parse", "HEAD")

    def remote_head():
        return git(tmp_path, "ls-remote", str(origin), f"refs/heads/{BRANCH}").split("\t")[0]

    def rows():
        path = tmp_path / ".agentihooks" / "swarm" / SLUG / "gates" / "log.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    rig = type("Rig", (), {})()
    rig.stop, rig.redis, rig.task, rig.ledger_task, rig.cli, rig.rows = stop, client, task, ledger_task, cli, rows
    rig.commit, rig.remote_head, rig.tree = commit, remote_head, tree
    try:
        cli("join")
        yield rig
    finally:
        subprocess.run([sys.executable, str(SCRIPTS / "ledger_server.py"), "--stop"], env=env, timeout=30)
        server.shutdown()
        server.server_close()


def test_unpushed_commits_reach_origin_and_the_task_until_the_agent_records_progress(rig):
    head = rig.commit()
    first = rig.stop()
    assert first.returncode == 2, (first.stderr, rig.rows())
    assert TEMPLATE in first.stderr
    assert rig.remote_head() == head
    comments = rig.ledger_task()["comments"]
    assert [(c["by"], c["text"]) for c in comments] == [
        ("swarm", f"The stop hook pushed the task branch {BRANCH} to origin")
    ]
    inbox = rig.redis.zrange(f"{KEY_ROOT}:inbox:address:{ME}", 0, -1)
    assert [rig.redis.hget(f"{KEY_ROOT}:inbox:item:{item}", "text") for item in inbox] == [TEMPLATE]
    rig.cli("comment", f"tasks/{rig.task}", "Pushed the first slice of the work.")
    second = rig.stop()
    assert second.returncode == 0, (second.stderr, rig.rows())


def test_dirty_work_is_refused_and_a_clean_pushed_worktree_with_its_pull_request_stops_freely(rig):
    rig.cli("task", "set", rig.task, f"pr_url={URL}")
    rig.commit()
    (rig.tree / "draft").write_text("draft\n")
    dirty = rig.stop()
    assert dirty.returncode == 2
    assert TEMPLATE in dirty.stderr
    assert [(r["gate"], r["kind"]) for r in rig.rows()] == [("push-stop", "deny")]
    (rig.tree / "draft").unlink()
    clean = rig.stop()
    assert clean.returncode == 0, clean.stderr
