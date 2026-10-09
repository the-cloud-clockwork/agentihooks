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
from scripts.swarm_ledger.repository import repository
from tests.swarm_ledger.test_ledger_authority import OTHER, SLUG, WORKER, admin_put, operation
from tests.swarm_ledger.test_remote_ledger_client import CREDENTIAL

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures/swarm_v2/remote-worker-home.json"
METRIC = "ledger_remote_auth_failures_total"
ISOLATION = ("REDIS_KEY_PREFIX", "AGENTIHOOKS_SWARM_REDIS_URL")
DRIVER = """
import json
import sys

from scripts.swarm_ledger import HERE, run

sys.path.insert(0, str(HERE))
import ledger

try:
    code = run(sys.argv[1:])
except SystemExit as exc:
    code = exc.code
print(json.dumps({"ledger_remote_auth_failures_total": ledger.ledger_remote_auth_failures_total()}), file=sys.stderr)
sys.exit(code)
"""


def inputs():
    return json.loads(FIXTURE.read_text())


def worker_home(root):
    shape = inputs()["home"]
    home = root / "home"
    for name in shape["present"]:
        (home / name).mkdir(parents=True)
    for path in (*(home / name for name in shape["present"]), home):
        path.chmod(int(shape["mode"], 8))
    assert os.geteuid() == 0 or not os.access(home, os.W_OK)
    return home


def restore(home):
    for path in (home, *(home / name for name in inputs()["home"]["present"])):
        path.chmod(0o755)


def listing(home):
    return sorted(str(path.relative_to(home)) for path in home.rglob("*"))


def remote_server():
    return patch.dict(os.environ, {"AGENTIHOOKS_DEPLOYMENT": inputs()["deployment"]})


def grant():
    pinned = {"AGENTIHOOKS_SWARM": "rig-grade-swarm", "AGENTIHOOKS_AGENT_NAME": WORKER}
    with remote_server(), patch.dict(os.environ, {**pinned, "AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL": CREDENTIAL}):
        return ledger.launch_token(SLUG, WORKER)


def remote_env(live, token, home=None, name=WORKER):
    env = {
        "AGENTIHOOKS_DEPLOYMENT": inputs()["deployment"],
        "LEDGER_URL": f"http://{live['host']}",
        "AGENTIHOOKS_SWARM": "rig-grade-swarm",
        "AGENTIHOOKS_AGENT_NAME": name,
    }
    if home is not None:
        env.update(HOME=str(home), AGENTIHOOKS_HOME=str(home / ".agentihooks"))
    if token:
        env["AGENTIHOOKS_LEDGER_AGENT_TOKEN"] = token
    return env


def worker(live, home, token, *argv, name=WORKER, extra=None):
    isolation = {key: os.environ[key] for key in ISOLATION if key in os.environ}
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(ROOT), **isolation, **remote_env(live, token, home, name)}
    done = subprocess.run(
        [sys.executable, "-c", DRIVER, "--slug", SLUG, "--as", name, *argv],
        env={**env, **(extra or {})},
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


def refused_as(done):
    return "unauthenticated" if done.returncode == 1 and "unauthenticated: " in done.stderr else done.stderr


def positive(live, root, run, token):
    home = worker_home(root)
    before = listing(home)
    update = inputs()["updates"][run]
    try:
        with remote_server():
            task = seed_task(live)
            joined, joined_total = worker(live, home, token, "join")
            shown, shown_total = worker(live, home, token, "show")
            posted, posted_total = worker(live, home, token, "comment", f"tasks/{task}", update)
    finally:
        restore(home)
    assert joined.returncode == 0, joined.stderr
    assert shown.returncode == 0, shown.stderr
    assert posted.returncode == 0, posted.stderr
    mine = [row for row in json.loads(shown.stdout)["tasks"] if row["id"] == task]
    expected = [(inputs()["task"]["title"], WORKER)]
    return {
        "task_read": [(row["title"], row["claimed_by"]) for row in mine] == expected,
        "update_committed": json.loads(posted.stdout) == {"posted": True} and comments(task) == [(WORKER, update)],
        "home_changed": listing(home) != before,
        "ledger_directory_created": any((home / name).exists() for name in inputs()["home"]["absent"]),
        METRIC: joined_total + shown_total + posted_total,
    }


def negative(live, root, run, token):
    home = worker_home(root)
    update = inputs()["updates"][run]
    local_credential = {"LEDGER_DIR": os.environ["LEDGER_DIR"]}
    try:
        with remote_server():
            task = seed_task(live)
            revision = repository.get_document(SLUG)["_meta"]["rev"]
            argv = ("comment", f"tasks/{task}", update)
            missing, missing_total = worker(live, home, None, *argv, extra=local_credential)
            foreign, foreign_total = worker(live, home, token, *argv, name=OTHER, extra=local_credential)
            changed = repository.get_document(SLUG)["_meta"]["rev"] != revision or comments(task) != []
            retried, retried_total = worker(live, home, token, *argv)
    finally:
        restore(home)
    output = "".join(done.stdout + done.stderr for done in (missing, foreign))
    assert retried_total == 0, retried.stderr
    return {
        "missing_token": refused_as(missing),
        "foreign_label": refused_as(foreign),
        "protected_state_changed": changed,
        "credential_disclosed": any(secret in output for secret in (live["admin"], token, CREDENTIAL)),
        "new_valid_request_posted": retried.returncode == 0 and comments(task) == [(WORKER, update)],
        METRIC: missing_total + foreign_total,
    }


def server_threads_only(name):
    real = getattr(repository, name)

    def guarded(slug):
        assert threading.current_thread() is not threading.main_thread(), f"the remote client called {name}"
        return real(slug)

    return guarded


def recovery(live, run, token):
    outage = inputs()["outage"]
    update = inputs()["updates"][run]
    real, reads, writes, sleeps = ledger.request, [], [], []

    def transient(slug, ops=None, service=False, timeout=ledger.REQUEST_TIMEOUT):
        (reads if ops is None else writes).append(ops)
        if ops is None and len(reads) <= outage["refused_reads"]:
            raise ConnectionRefusedError("ledger outage")
        reply = real(slug, ops, service, timeout)
        if ops is not None and len(writes) == 1:
            raise ConnectionResetError("response lost")
        return reply

    entry = {"op": "add", "id": f"c-{uuid.uuid4().hex[:10]}", "text": update, "by": WORKER}
    before = ledger.ledger_remote_auth_failures_total()
    with (
        remote_server(),
        patch.dict(os.environ, remote_env(live, token)),
        patch.object(ledger, "request", transient),
        patch.object(ledger.time, "sleep", sleeps.append),
        patch.object(ledger.subprocess, "run") as started,
        patch.object(repository, "exists", server_threads_only("exists")),
        patch.object(repository, "token", server_threads_only("token")),
    ):
        os.environ.pop("LEDGER_AUTOSTART", None)
        task = seed_task(live)
        entry["thread"] = f"tasks/{task}/comments"
        with pytest.raises(SystemExit, match="^ledger server not answering on .*: response lost$"):
            ledger.call(SLUG, [dict(entry)])
        applied = comments(task) == [(WORKER, update)]
        state = ledger.call(SLUG)
        replayed = [ledger.call(SLUG, [dict(entry)]) for _ in range(2)]
    assert any(row["id"] == task for row in state["tasks"])
    assert sleeps == [0.5, 1.0]
    assert all(not reply["rejected"] for reply in replayed)
    return {
        "read_attempts": len(reads),
        "write_attempts": len(writes),
        "servers_started": started.call_count,
        "lost_response_applied": applied,
        "replayed_effects": len(comments(task)),
        METRIC: ledger.ledger_remote_auth_failures_total() - before,
    }
