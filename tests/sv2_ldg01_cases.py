import hashlib
import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.swarm_ledger import ledger
from scripts.swarm_ledger import ledger_authority as authority
from scripts.swarm_ledger.repository import repository
from tests.swarm_ledger.test_ledger_authority import SLUG, WORKER, admin_put, operation

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures/swarm_v2/remote-worker-home.json"
METRIC = "ledger_remote_auth_failures_total"
DRIVER = """
import json
import sys

from scripts.swarm_ledger import run

try:
    code = run(sys.argv[1:])
except SystemExit as exc:
    code = exc.code
total = sys.modules["ledger"].ledger_remote_auth_failures_total()
print(json.dumps({"ledger_remote_auth_failures_total": total}), file=sys.stderr)
sys.exit(code)
"""


def inputs():
    return json.loads(FIXTURE.read_text())


def worker_home(root):
    home = root / "home"
    (home / ".agentihooks").mkdir(parents=True)
    for path in (home / ".agentihooks", home):
        path.chmod(0o555)
    return home


def restore(home):
    for path in (home, home / ".agentihooks"):
        path.chmod(0o755)


def listing(home):
    return sorted(str(path.relative_to(home)) for path in home.rglob("*"))


def remote_env(live, home=None, token=True):
    env = {
        "AGENTIHOOKS_DEPLOYMENT": inputs()["deployment"],
        "LEDGER_URL": f"http://{live['host']}",
        "AGENTIHOOKS_SWARM": "rig-grade-swarm",
        "AGENTIHOOKS_AGENT_NAME": WORKER,
    }
    if home is not None:
        env.update(HOME=str(home), AGENTIHOOKS_HOME=str(home / ".agentihooks"))
    if token:
        env["AGENTIHOOKS_LEDGER_AGENT_TOKEN"] = authority.agent_token(live["admin"], SLUG, WORKER)
    return env


def worker(live, home, *argv, token=True, extra=None):
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(ROOT), **remote_env(live, home, token), **(extra or {})}
    done = subprocess.run(
        [sys.executable, "-c", DRIVER, "--slug", SLUG, "--as", WORKER, *argv],
        env=env,
        cwd=home,
        capture_output=True,
        text=True,
        timeout=120,
    )
    lines = [line for line in done.stderr.splitlines() if line.startswith('{"' + METRIC)]
    assert lines, done.stderr
    return done, json.loads(lines[0])[METRIC]


def seed_task(live):
    task = f"t{uuid.uuid4().int % 10**9}"
    added = operation("task_add", by="swarm", task=task, not_duplicate="fixture task", **inputs()["task"])
    fields = {"state": "claimed", "claimed_by": WORKER}
    claimed = operation("task_update", by="swarm", item=f"tasks/{task}", fields=fields)
    status, reply = admin_put(live, added, claimed)
    assert (status, reply["rejected"]) == (200, []), reply
    return task


def stored_task(task):
    return next(row for row in repository.get_document(SLUG)["tasks"] if row["id"] == task)


def comments(task):
    return [(entry["by"], entry["text"]) for entry in stored_task(task).get("comments", [])]


def positive(live, root, run):
    home = worker_home(root)
    before = listing(home)
    task = seed_task(live)
    update = inputs()["updates"][run]
    try:
        joined, joined_total = worker(live, home, "join")
        shown, shown_total = worker(live, home, "show")
        posted, posted_total = worker(live, home, "comment", f"tasks/{task}", update)
    finally:
        restore(home)
    assert joined.returncode == 0, joined.stderr
    assert shown.returncode == 0, shown.stderr
    mine = next(row for row in json.loads(shown.stdout)["tasks"] if row["id"] == task)
    assert (mine["title"], mine["claimed_by"]) == (inputs()["task"]["title"], WORKER)
    assert (posted.returncode, json.loads(posted.stdout)) == (0, {"posted": True}), posted.stderr
    assert comments(task) == [(WORKER, update)]
    assert listing(home) == before
    assert not (home / "development-ledger").exists()
    return {
        "task_read": True,
        "update_committed": True,
        "home_changed": False,
        "ledger_directory_created": False,
        METRIC: joined_total + shown_total + posted_total,
    }


def negative(live, root, run):
    home = worker_home(root)
    task = seed_task(live)
    update = inputs()["updates"][run]
    revision = repository.get_document(SLUG)["_meta"]["rev"]
    local_credential = {"LEDGER_DIR": os.environ["LEDGER_DIR"]}
    try:
        argv = ("comment", f"tasks/{task}", update)
        refused, refused_total = worker(live, home, *argv, token=False, extra=local_credential)
        unchanged = repository.get_document(SLUG)["_meta"]["rev"] == revision and comments(task) == []
        retried, retried_total = worker(live, home, *argv)
    finally:
        restore(home)
    assert refused.returncode == 1
    assert "unauthenticated" in refused.stderr, refused.stderr
    disclosed = live["admin"] in refused.stdout + refused.stderr
    assert not disclosed
    assert refused_total == 1
    assert unchanged
    assert (retried.returncode, retried_total, comments(task)) == (0, 0, [(WORKER, update)]), retried.stderr
    return {
        "unauthenticated": True,
        "local_credential_used": False,
        "protected_state_changed": False,
        "credential_disclosed": disclosed,
        "new_valid_request_required": True,
        METRIC: refused_total,
    }


def main_thread_only(name):
    real = getattr(repository, name)

    def guarded(slug):
        assert threading.current_thread() is not threading.main_thread(), f"the remote client called {name}"
        return real(slug)

    return guarded


def recovery(live, run):
    outage = inputs()["outage"]
    task = seed_task(live)
    update = inputs()["updates"][run]
    real, reads, writes, sleeps = ledger.request, [], [], []

    def transient(slug, ops=None, service=False, timeout=10):
        (reads if ops is None else writes).append(ops)
        if ops is not None and len(writes) == 1 or ops is None and len(reads) <= outage["refused_reads"]:
            raise ConnectionRefusedError("ledger outage")
        return real(slug, ops, service, timeout)

    entry = {"op": "add", "thread": f"tasks/{task}/comments", "id": f"c-{uuid.uuid4().hex[:10]}"}
    entry.update(text=update, by=WORKER)
    before = ledger.ledger_remote_auth_failures_total()
    with (
        patch.dict(os.environ, remote_env(live)),
        patch.object(ledger, "request", transient),
        patch.object(ledger.time, "sleep", sleeps.append),
        patch.object(ledger.subprocess, "run") as started,
        patch.object(repository, "exists", main_thread_only("exists")),
        patch.object(repository, "token", main_thread_only("token")),
    ):
        os.environ.pop("LEDGER_AUTOSTART", None)
        with pytest.raises(SystemExit, match="not answering"):
            ledger.call(SLUG, [dict(entry)])
        failed_write_applied = comments(task) != []
        state = ledger.call(SLUG)
        replayed = [ledger.call(SLUG, [dict(entry)]) for _ in range(2)]
    assert not failed_write_applied
    assert any(row["id"] == task for row in state["tasks"])
    assert (len(reads), len(writes), started.call_count) == (outage["read_attempts"], 3, 0)
    assert sleeps == [ledger.REMOTE_READ_PAUSE, 2 * ledger.REMOTE_READ_PAUSE]
    assert all(not reply["rejected"] for reply in replayed)
    assert comments(task) == [(WORKER, update)]
    return {
        "read_attempts": len(reads),
        "failed_write_attempts": 1,
        "servers_started": started.call_count,
        "failed_write_applied": failed_write_applied,
        "replayed_effects": len(comments(task)),
        METRIC: ledger.ledger_remote_auth_failures_total() - before,
    }


def manifest():
    return {FIXTURE.name: hashlib.sha256(FIXTURE.read_bytes()).hexdigest()}
