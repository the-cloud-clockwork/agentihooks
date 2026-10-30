import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from scripts.swarm_ledger.api import store
from scripts.swarm_ledger.repository import plain, remote, sqlite
from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "scripts" / "swarm_ledger" / "ledger_server.py"
CONTENT = {
    "title": "Split",
    "overview": "o",
    "sources": [],
    "phases": [{"title": "One", "description": "d"}],
    "tasks": [{"title": "first", "phase": "p1", "lane": "eng"}],
}
ADD = {"op": "task_add", "id": "a1", "by": "swarm", "task": "t2", "title": "second", "lane": "eng", "phase": "p1"}
ADD_OTHER = {**ADD, "id": "a2", "task": "t3", "title": "third"}
FORBIDDEN_NAMES = {"ledger_core", "scripts.swarm"}
FORBIDDEN_PREFIXES = ("scripts.gates", "scripts.swarm.", "scripts.hub", "ledger_", "scripts.swarm_ledger.ledger_core")

PROBE = """
import json, sys, threading, urllib.request, urllib.error
from scripts.swarm_ledger.api import store

stored = store.repository()
listening = store.server(stored, "127.0.0.1", 0, "")
threading.Thread(target=listening.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{listening.server_address[1]}/api/v1/store"

def call(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=data, method=method)
    try:
        with urllib.request.urlopen(request) as answer:
            return answer.status
    except urllib.error.HTTPError as answer:
        return answer.code

status, revision = 0, 0
with urllib.request.urlopen(base + "/ledgers/split") as answer:
    revision = json.loads(answer.read())["state"]["_meta"]["rev"]
statuses = [
    call("GET", "/ledgers"),
    call("GET", "/ledgers/split/read?read=title"),
    call("GET", "/ledgers/split/token"),
    call("GET", "/ledgers/split/events?events=0"),
    call("GET", "/ledgers/split/nodes?read=children"),
    call("POST", "/ledgers/split/commit", dict(revision=revision, changed={"title": "Moved"}, dropped=[], events=[], summary={"slug": "split"})),
    call("PUT", "/files/media_files/split/m1", {"data": "aGk=", "at": 1}),
    call("GET", "/files/media_files/split/m1"),
    call("POST", "/held", {"action": "hold", "kind": "inbox", "slug": "split", "revision": 1}),
    call("GET", "/held"),
]
print(json.dumps({"statuses": statuses, "modules": sorted(sys.modules)}))
"""


def seeded(folder: Path) -> SQLiteLedgerRepository:
    repository = SQLiteLedgerRepository(folder / sqlite.DATABASE)
    repository.create("split", CONTENT)
    return repository


def forbidden(modules: list[str]) -> list[str]:
    return [name for name in modules if name in FORBIDDEN_NAMES or name.startswith(FORBIDDEN_PREFIXES)]


def child_env(folder: Path, **extra: str) -> dict:
    env = {key: value for key, value in os.environ.items() if key != "AGENTIHOOKS_SWARM_DATABASE_URL"}
    return {**env, "PYTHONPATH": str(ROOT), "LEDGER_DIR": str(folder), "AGENTIHOOKS_BUS": "memory", **extra}


@pytest.fixture
def api(tmp_path):
    folder = tmp_path / "api"
    stored = seeded(folder)
    listening = store.server(store.Folder(folder / sqlite.DATABASE, store.plain), "127.0.0.1", 0, "secret")
    threading.Thread(target=listening.serve_forever, daemon=True).start()
    try:
        yield stored, f"http://127.0.0.1:{listening.server_address[1]}"
    finally:
        listening.shutdown()
        listening.server_close()


def test_the_api_process_serves_every_route_without_importing_a_rule_gate_or_swarm_module(tmp_path):
    folder = tmp_path / "api"
    seeded(folder)

    result = subprocess.run(
        [sys.executable, "-c", PROBE], env=child_env(folder), capture_output=True, text=True, timeout=60
    )

    assert result.returncode == 0, result.stderr
    found = json.loads(result.stdout.splitlines()[-1])
    assert found["statuses"] == [200] * len(found["statuses"])
    assert forbidden(found["modules"]) == []


def test_a_commit_ruled_against_a_revision_the_ledger_moved_past_is_refused_and_writes_nothing(api):
    stored, base = api
    revision = stored.current("split").state["_meta"]["rev"]
    client = remote.Client(base, "secret")
    body = dict(revision=revision - 1, changed={"title": "Stale"}, dropped=[], events=[], summary={"slug": "split"})

    status, answer = client.call("POST", "/ledgers/split/commit", body=body)

    assert (status, answer) == (409, {"code": "revision_conflict", "revision": revision})
    assert stored.read("split", "title") == {"title": "Split"}


def test_the_api_refuses_a_caller_without_its_token(api):
    _, base = api

    status, answer = remote.Client(base, "wrong").call("GET", "/ledgers")

    assert (status, answer) == (401, {"code": "unauthorized"})


def test_the_remote_repository_rules_ops_itself_and_commits_them_through_the_api(api):
    stored, base = api
    repository = remote.RemoteLedgerRepository(remote.Client(base, "secret"))

    state, rejected = repository.apply_ops("split", ops=[ADD])

    assert rejected == []
    assert [task["id"] for task in state["tasks"]] == ["t1", "t2"]
    assert [task["id"] for task in stored.current("split").state["tasks"]] == ["t1", "t2"]
    assert repository.exists("split") and not repository.exists("absent")
    assert repository.read("split", "title") == {"title": "Split"}
    assert [summary["slug"] for summary in repository.list_summaries()] == ["split"]


def test_the_remote_repository_reruns_the_rules_on_a_fresh_read_when_another_writer_moved_the_ledger(api):
    stored, base = api
    repository = remote.RemoteLedgerRepository(remote.Client(base, "secret"))
    repository.current("split")
    stored.apply_ops("split", ops=[ADD_OTHER])

    state, rejected = repository.apply_ops("split", ops=[ADD])

    assert rejected == []
    assert [task["id"] for task in state["tasks"]] == ["t1", "t3", "t2"]
    assert [task["id"] for task in stored.current("split").state["tasks"]] == ["t1", "t3", "t2"]


def test_the_remote_repository_stops_after_three_conflicting_commits(api, monkeypatch):
    _, base = api
    repository = remote.RemoteLedgerRepository(remote.Client(base, "secret"))
    commits = []

    def conflicting(slug, revision, before, state, events):
        commits.append(revision)
        raise sqlite.Conflict(slug, revision + 1)

    monkeypatch.setattr(repository, "commit", conflicting)

    with pytest.raises(sqlite.Conflict):
        repository.apply_ops("split", ops=[ADD])
    assert len(commits) == remote.ATTEMPTS


def test_storage_builds_the_remote_repository_only_when_the_api_is_set_and_the_database_is_not(monkeypatch):
    from scripts.swarm_ledger import storage

    monkeypatch.delenv("AGENTIHOOKS_SWARM_DATABASE_URL", raising=False)
    monkeypatch.setenv(remote.URL_ENV, "http://api:8766")
    assert isinstance(storage.build(), remote.RemoteLedgerRepository)
    monkeypatch.delenv(remote.URL_ENV)
    assert isinstance(storage.build(), SQLiteLedgerRepository)


def until(check, limit: float = 30.0) -> None:
    deadline = time.monotonic() + limit
    while not check():
        assert time.monotonic() < deadline
        time.sleep(0.05)


def answering(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as answer:
            return answer.status == 200
    except OSError:
        return False


@pytest.mark.wall_clock
def test_a_page_request_on_the_api_backend_is_answered_without_a_database_of_its_own(api, tmp_path, ledger_port):
    _, base = api
    folder = tmp_path / "ledger"
    folder.mkdir()
    env = child_env(
        folder,
        LEDGER_PORT=str(ledger_port),
        SWARM_RELOAD="0",
        AGENTIHOOKS_SWARM_API_URL=base,
        AGENTIHOOKS_SWARM_API_TOKEN="secret",
    )
    with (tmp_path / "server.out").open("w") as log:
        server = subprocess.Popen([sys.executable, str(SERVER), "--serve"], env=env, stdout=log, stderr=log)
    try:
        until(lambda: answering(ledger_port) or server.poll() is not None)
        assert server.poll() is None, (tmp_path / "server.out").read_text()
        with urllib.request.urlopen(f"http://127.0.0.1:{ledger_port}/split", timeout=10) as answer:
            page = answer.read().decode()
    finally:
        server.send_signal(signal.SIGTERM)
        server.wait(timeout=30)

    assert "<title>Split" in page
    assert not (folder / sqlite.DATABASE).exists()


def test_the_api_domain_reads_the_ledger_folder_from_the_environment_and_counts_milliseconds(monkeypatch, tmp_path):
    import importlib

    monkeypatch.setenv("LEDGER_DIR", str(tmp_path))
    reloaded = importlib.reload(plain)

    assert reloaded.LEDGER_DIR == tmp_path
    assert abs(reloaded.now_ms() - time.time() * 1000) < 5000


def test_a_registry_write_commits_on_a_clean_exit_and_is_refused_once_another_writer_moved_it(api):
    stored, base = api
    repository = remote.RemoteLedgerRepository(remote.Client(base, "secret"))
    with repository.connect() as session:
        found = repository.registry("bin", session)
        repository.save_registry(session, "bin", {**found, "split": 5})

    with stored.connect() as connection:
        assert stored.registry("bin", connection) == {"split": 5}

    with pytest.raises(remote.RegistryConflict), repository.connect() as session:
        repository.registry("bin", session)
        with stored.connect() as connection, connection:
            stored.save_registry(connection, "bin", {})
        repository.save_registry(session, "bin", {"other": 1})


def test_a_registry_saved_without_reading_it_first_is_refused(api):
    _, base = api
    client = remote.Client(base, "secret")

    status, answer = client.call("POST", "/registries", body=dict(expected={}, set={"bin": {}}, purge=[]))

    assert (status, answer) == (400, {"code": "bad_request", "registries": ["bin"]})


def test_a_quoted_slug_reaches_the_ledger_it_names(api):
    _, base = api
    client = remote.Client(base, "secret")

    status, answer = client.call("GET", "/ledgers/%73plit/read", {"read": ["title"]})

    assert (status, answer) == (200, {"title": "Split"})


def test_an_oversized_body_is_refused_before_it_is_read(api):
    _, base = api
    request = urllib.request.Request(
        f"{base}/api/v1/store/held",
        data=b"{}",
        method="POST",
        headers={"Authorization": "Bearer secret", "Content-Length": str(store.MAX_BODY + 1)},
    )

    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(request, timeout=10)
    assert refused.value.code == 400


def test_the_api_refuses_to_start_without_a_token_unless_told_to_serve_open(monkeypatch):
    monkeypatch.delenv(store.TOKEN_ENV, raising=False)

    with pytest.raises(SystemExit) as stopped:
        store.main(["--port", "0"])
    assert stopped.value.code == 2
