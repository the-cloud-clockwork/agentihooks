import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path

import pytest

from scripts.swarm_ledger import storage
from scripts.swarm_ledger.api import store
from scripts.swarm_ledger.repository import artifact_files, media_files, plain, remote, sqlite
from scripts.swarm_ledger.repository.entry import Entry
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
RULES = (
    "ledger_core",
    "ledger_alerts",
    "ledger_plans",
    "ledger_priorities",
    "ledger_notifications",
    "ledger_close",
    "ledger_bin",
    "ledger_artifacts",
    "ledger_media",
    "ledger_authority",
    "ledger_tasks",
    "ledger_comments",
)
FORBIDDEN_NAMES = {
    "scripts.swarm",
    "scripts.swarm_ledger.repository.mutation",
    "scripts.swarm_ledger.repository.legacy",
    *RULES,
    *(f"scripts.swarm_ledger.{name}" for name in RULES),
}
FORBIDDEN_PREFIXES = ("scripts.gates", "scripts.swarm.", "scripts.hub")

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
    listening = store.server(storage.owned(store.plain, folder / sqlite.DATABASE), "127.0.0.1", 0, "secret")
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


def test_the_remote_repository_reruns_at_most_three_times_after_a_conflicting_commit(api, monkeypatch):
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


def test_the_api_refuses_to_start_without_a_token_unless_told_to_serve_open(monkeypatch, capsys):
    monkeypatch.delenv(store.TOKEN_ENV, raising=False)

    with pytest.raises(SystemExit) as stopped:
        store.main(["--port", "0"])
    assert stopped.value.code == 2
    assert capsys.readouterr().err.splitlines()[-1] == (
        f"agentihooks-swarm-api: error: {store.TOKEN_ENV} is unset; pass --open to serve without a token"
    )


def test_the_api_refuses_to_start_without_the_shared_database_unless_told_to_serve_a_folder(monkeypatch):
    monkeypatch.setenv(store.TOKEN_ENV, "secret")
    monkeypatch.delenv("AGENTIHOOKS_SWARM_DATABASE_URL", raising=False)

    with pytest.raises(SystemExit) as stopped:
        store.main(["--port", "0"])
    assert stopped.value.code == 2


@pytest.mark.parametrize(
    "broken",
    [{"revision": "1"}, {"changed": []}, {"dropped": ["_meta"]}, {"changed": {"_meta": []}}],
)
def test_a_commit_of_the_wrong_shape_is_refused_and_writes_nothing(api, broken):
    stored, base = api
    revision = stored.current("split").state["_meta"]["rev"]
    body = {**dict(revision=revision, changed={"title": "Moved"}, dropped=[], events=[], summary={}), **broken}

    status, answer = remote.Client(base, "secret").call("POST", "/ledgers/split/commit", body=body)

    assert status == 400 and answer["code"] == "bad_request"
    assert stored.read("split", "title") == {"title": "Split"}


@contextmanager
def serving(stored: object, token: str) -> Iterator[str]:
    listening = store.server(stored, "127.0.0.1", 0, token)
    threading.Thread(target=listening.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{listening.server_address[1]}"
    finally:
        listening.shutdown()
        listening.server_close()


def raw(base: str, request: bytes) -> bytes:
    host, port = base.removeprefix("http://").split(":")
    data = b""
    with socket.create_connection((host, int(port)), timeout=5) as connection:
        connection.sendall(request)
        connection.settimeout(1)
        try:
            while chunk := connection.recv(65536):
                data += chunk
        except TimeoutError:
            pass
    return data


def offline() -> remote.RemoteLedgerRepository:
    return remote.RemoteLedgerRepository(remote.Client("http://unused"))


def test_the_store_answers_health_and_refuses_paths_outside_its_prefix(api: tuple) -> None:
    _, base = api

    with urllib.request.urlopen(f"{base}/healthz", timeout=10) as answer:
        health = json.loads(answer.read())
    with pytest.raises(urllib.error.HTTPError) as outside:
        urllib.request.urlopen(f"{base}/elsewhere", timeout=10)

    assert health == {"ok": True}
    assert (outside.value.code, json.loads(outside.value.read())) == (404, {"code": "not_found"})


def test_the_store_names_why_it_refuses_an_unknown_route_ledger_or_action(api: tuple) -> None:
    _, base = api
    client = remote.Client(base, "secret")

    assert client.call("GET", "/bogus") == (404, {"code": "not_found"})
    assert client.call("GET", "/ledgers/split/bogus") == (404, {"code": "not_found"})
    assert client.call("GET", "/files/unknown/split") == (404, {"code": "not_found"})
    assert client.call("POST", "/files/media_files/split/m1", body={}) == (404, {"code": "not_found"})
    assert client.call("GET", "/ledgers/absent/token") == (404, {"code": "ledger_not_found"})
    assert client.call("POST", "/held", body={"action": "bogus"}) == (400, {"code": "unknown_action"})


def test_a_read_at_the_generation_the_caller_holds_answers_not_modified_with_no_body(api: tuple) -> None:
    _, base = api
    client = remote.Client(base, "secret")
    _, found = client.call("GET", "/ledgers/split")
    request = (
        f"GET /api/v1/store/ledgers/split?read={found['generation']} HTTP/1.1\r\nHost: api\r\n"
        "Authorization: Bearer secret\r\nConnection: close\r\n\r\n"
    )

    assert client.call("GET", "/ledgers/split", {"read": found["generation"]}) == (304, None)
    data = raw(base, request.encode())
    assert data.startswith(b"HTTP/1.1 304") and data.endswith(b"Content-Length: 0\r\n\r\n")


def test_a_refused_request_closes_its_connection(api: tuple) -> None:
    _, base = api
    refused = b"GET /api/v1/store/bogus HTTP/1.1\r\nHost: api\r\nAuthorization: Bearer secret\r\n\r\n"

    data = raw(base, refused + b"GET /healthz HTTP/1.1\r\nHost: api\r\n\r\n")

    assert data.startswith(b"HTTP/1.1 404") and data.count(b"HTTP/1.1 ") == 1


def test_a_store_without_a_token_serves_every_caller_and_a_failing_store_answers_store_failed(tmp_path: Path) -> None:
    class Failing:
        def summaries(self) -> list:
            raise RuntimeError("down")

    with serving(Failing(), "") as base:
        failed = remote.Client(base).call("GET", "/ledgers")
    with serving(seeded(tmp_path / "open"), "") as base:
        status, _ = remote.Client(base).call("GET", "/ledgers")

    assert failed == (500, {"code": "store_failed", "detail": "RuntimeError"})
    assert status == 200


def test_a_commit_that_drops_the_meta_record_names_it(api: tuple) -> None:
    stored, base = api
    revision = stored.current("split").state["_meta"]["rev"]
    body = dict(revision=revision, changed={}, dropped=["_meta"], events=[], summary={})

    answer = remote.Client(base, "secret").call("POST", "/ledgers/split/commit", body=body)

    assert answer == (400, {"code": "bad_request", "fields": ["_meta"]})


def test_a_commit_drops_the_keys_it_names_and_stores_the_summary_it_carries(api: tuple) -> None:
    stored, base = api
    revision = stored.current("split").state["_meta"]["rev"]
    summary = {**stored.summaries()[0], "title": "Given"}
    body = dict(revision=revision, changed={"title": "Moved"}, dropped=["overview"], events=[], summary=summary)

    status, _ = remote.Client(base, "secret").call("POST", "/ledgers/split/commit", body=body)

    assert status == 200
    assert "overview" not in stored.current("split").state
    assert stored.summaries()[0]["title"] == "Given"


def test_a_commit_adopts_only_the_legacy_files_of_the_ledger_it_writes(tmp_path: Path) -> None:
    folder = tmp_path / "api"
    stored = seeded(folder)
    revision = stored.current("split").state["_meta"]["rev"]
    summary = stored.summaries()[0]
    legacy = folder / "other.json"
    legacy.write_text(json.dumps(stored.get_document("split")))

    stored.commit("split", revision, {"title": "Moved"}, [], [], summary)

    assert legacy.exists()


def test_storage_points_the_remote_repository_at_the_api_with_its_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTIHOOKS_SWARM_DATABASE_URL", raising=False)
    monkeypatch.setenv(remote.URL_ENV, "http://api:8766/")
    monkeypatch.setenv(remote.TOKEN_ENV, "secret")

    client = storage.build().client

    assert (client.base, client.token) == ("http://api:8766/api/v1/store", "secret")


def test_the_store_owner_serves_the_shared_database_when_its_url_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    built = []
    address = "postgresql://ledger@db.test/ledger"
    monkeypatch.setattr(storage, "shared", lambda: address)
    monkeypatch.setattr(
        storage.postgres, "PostgresLedgerRepository", lambda given, domain: built.append((given, domain)) or "built"
    )

    assert storage.owned(plain) == "built"
    assert built == [(address, plain)]
    folder = storage.owned(plain, Path("/unused/ledgers.sqlite3"))
    assert (type(folder), folder.domain, folder.adopts) == (SQLiteLedgerRepository, plain, False)


class Listening:
    server_address = ("0.0.0.0", 4321)

    def __init__(self) -> None:
        self.polls: list = []
        self.closed = False

    def serve_forever(self, poll: float) -> None:
        self.polls.append(poll)

    def server_close(self) -> None:
        self.closed = True


def test_the_api_serves_its_repository_on_every_interface_until_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    listening, served = Listening(), []
    monkeypatch.delenv(store.TOKEN_ENV, raising=False)
    monkeypatch.setattr(store.storage, "shared", lambda: "postgresql://ledger@db.test/ledger")
    monkeypatch.setattr(store, "repository", lambda: "stored")
    monkeypatch.setattr(store, "server", lambda *given: served.append(given) or listening)

    assert store.main(["--port", "0", "--open", "--no-relay"]) == 0
    assert served == [("stored", "0.0.0.0", 0, "")]
    assert listening.polls == [0.2] and listening.closed


def test_a_registry_commit_answers_ok_and_a_conflict_names_the_registries_that_moved(api: tuple) -> None:
    stored, base = api
    client = remote.Client(base, "secret")
    first = dict(expected={"bin": {}}, set={"bin": {"split": 5}}, purge=[])
    second = dict(expected={"bin": {"split": 5}}, set={"bin": {"split": 6}}, purge=[])

    assert client.call("POST", "/registries", body=first) == (200, {"ok": True})
    assert client.call("POST", "/registries", body=second) == (200, {"ok": True})
    session = remote.Session(client)
    session.registry("bin")
    with stored.connect() as connection, connection:
        stored.save_registry(connection, "bin", {})
    session.saved["bin"] = {}
    with pytest.raises(remote.RegistryConflict) as conflict:
        session.commit()
    assert conflict.value.names == ["bin"]


def test_a_registry_session_takes_only_transaction_openers_and_file_statements() -> None:
    session = remote.Session(remote.Client("http://unused"))

    opened = session.execute(sqlite.BEGIN)
    assert (opened.fetchone(), opened.fetchall()) == (None, [])
    with pytest.raises(remote.Unavailable) as refused:
        session.execute("SELECT 1")
    assert str(refused.value) == "SELECT 1"


def test_a_session_sends_each_file_statement_to_its_table_route(monkeypatch: pytest.MonkeyPatch) -> None:
    session = remote.Session(remote.Client("http://unused"))
    sent = []

    def answer(method: str, path: str, query: dict | None = None, body: object = None) -> tuple[int, object]:
        sent.append((method, path, body))
        return 200, {"data": "aGk="} if method == "GET" and path.count("/") == 4 else {"m1": 7}

    monkeypatch.setattr(session.client, "call", answer)

    assert session.execute(artifact_files.SAVE, ("split", "a1", "aGk=")).fetchall() == []
    assert session.execute(media_files.SAVE, ("split", "m1", "aGk=", 7)).fetchall() == []
    assert session.execute(media_files.LOAD, ("split", "m1")).fetchone() == ("aGk=",)
    assert session.execute(artifact_files.IDS, ("split",)).fetchall() == [("m1", 7)]
    assert session.execute(media_files.AGES, ("split",)).fetchall() == [("m1", 7)]
    assert session.execute(artifact_files.DROP, ("split", "a1")).fetchall() == []
    assert sent == [
        ("PUT", "/files/artifact_files/split/a1", {"data": "aGk=", "at": None}),
        ("PUT", "/files/media_files/split/m1", {"data": "aGk=", "at": 7}),
        ("GET", "/files/media_files/split/m1", None),
        ("GET", "/files/artifact_files/split", None),
        ("GET", "/files/media_files/split", None),
        ("DELETE", "/files/artifact_files/split/a1", None),
    ]


def test_a_file_load_the_store_fails_raises_unavailable_with_its_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    session = remote.Session(remote.Client("http://unused"))
    monkeypatch.setattr(session.client, "call", lambda *given, **named: (500, {"code": "store_failed"}))

    with pytest.raises(remote.Unavailable) as failed:
        session.execute(artifact_files.LOAD, ("split", "a1"))
    assert str(failed.value) == "500 {'code': 'store_failed'}"


def test_a_purge_in_a_registry_session_removes_the_ledger_and_its_cached_copy(api: tuple) -> None:
    stored, base = api
    repository = remote.RemoteLedgerRepository(remote.Client(base, "secret"))
    repository.current("split")

    with repository.connect() as session:
        repository.purge("split", session)

    assert repository.generations() == {}
    assert not stored.exists("split")


def test_a_ledger_gone_from_the_store_leaves_the_cache_and_reads_as_missing(api: tuple) -> None:
    stored, base = api
    repository = remote.RemoteLedgerRepository(remote.Client(base, "secret"))
    repository.current("split")
    with stored.connect() as connection, connection:
        stored.purge("split", connection)

    with pytest.raises(sqlite.Missing):
        repository.current("split")
    assert repository.generations() == {}


def test_the_cache_keeps_the_newest_generation_it_has_seen() -> None:
    repository = offline()
    newer = Entry(5, None, {"title": "new"})

    assert repository._remember("split", newer) is newer
    assert repository._remember("split", Entry(3, None, {"title": "old"})) is newer
    assert repository._remember("split", Entry(5, None, {"title": "same"})) is newer


def test_the_remote_repository_applies_checkbox_changes_and_hands_back_a_private_copy(api: tuple) -> None:
    _, base = api
    repository = remote.RemoteLedgerRepository(remote.Client(base, "secret"))

    state, rejected = repository.apply_ops("split", changes=[{"path": "phases/p1/done", "value": True, "base": False}])
    state["title"] = "Mutated"

    assert rejected == [] and state["phases"][0]["done"] is True
    assert repository.current("split").state["title"] == "Split"


def test_the_last_rerun_still_carries_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    repository, gates = offline(), []

    def ruled(slug: str, changes: list | None, ops: list | None, gate: object, copy_state: bool) -> tuple:
        gates.append((changes, ops, gate, copy_state))
        if len(gates) < remote.ATTEMPTS:
            raise sqlite.Conflict(slug, 1)
        return {}, []

    monkeypatch.setattr(repository, "_ruled", ruled)

    repository.apply_ops("split", ["c1"], ops=[ADD], gate="gate", copy_state=False)
    assert gates == [(["c1"], [ADD], "gate", False)] * remote.ATTEMPTS
    assert str(sqlite.Conflict("split", 3)) == "ledger split is at revision 3"


def test_a_remote_commit_against_a_moved_revision_raises_conflict_naming_the_stored_revision(api: tuple) -> None:
    _, base = api
    repository = remote.RemoteLedgerRepository(remote.Client(base, "secret"))
    entry = repository.current("split")
    revision = entry.state["_meta"]["rev"]

    with pytest.raises(sqlite.Conflict) as conflict:
        repository.commit("split", revision - 1, entry.state, {**entry.state, "title": "Stale"}, [])
    assert (conflict.value.slug, conflict.value.revision) == ("split", revision)


def test_a_remote_commit_the_store_fails_raises_unavailable_with_its_answer(
    api: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, base = api
    repository = remote.RemoteLedgerRepository(remote.Client(base, "secret"))
    entry = repository.current("split")
    monkeypatch.setattr(repository.client, "call", lambda *given, **named: (500, {"code": "store_failed"}))

    with pytest.raises(remote.Unavailable) as failed:
        repository.commit("split", 1, entry.state, {**entry.state, "title": "Moved"}, [])
    assert str(failed.value) == "500 {'code': 'store_failed'}"


def test_views_and_task_rows_of_the_cached_ledger_are_built_once_per_generation(api: tuple) -> None:
    _, base = api
    repository = remote.RemoteLedgerRepository(remote.Client(base, "secret"))
    repository.apply_ops("split", ops=[ADD])
    generation, exported = repository.export_since("split", 0, lambda fragments: fragments.value["title"], None)
    assert (generation, exported) == (repository.generations()["split"], "Split")
    rows = repository.task_rows("split")
    assert rows[0] == generation and [task["id"] for task in rows[2]] == ["t1", "t2"]
    assert repository.task_rows("split", rows) is rows


def test_events_and_hierarchy_reads_through_the_api_match_the_store(api: tuple) -> None:
    stored, base = api
    repository = remote.RemoteLedgerRepository(remote.Client(base, "secret"))
    repository.apply_ops("split", ops=[ADD])
    revision = stored.current("split").state["_meta"]["rev"]
    repository.apply_ops("split", ops=[ADD_OTHER])

    root = repository.nodes("split", "children")
    child = repository.nodes("split", "children", root[0]["node"])

    assert repository.events_since("split", revision) == stored.events_since("split", revision)
    assert len(repository.events_since("split", 0)) > len(repository.events_since("split", revision))
    assert root == stored.nodes("split", "children")
    assert child == stored.nodes("split", "children", root[0]["node"]) and child != root
    assert remote.Client(base, "secret").call("GET", "/ledgers/split/nodes") == (200, root)


def test_held_revisions_are_held_released_and_dropped_through_the_api(api: tuple) -> None:
    stored, base = api
    repository = remote.RemoteLedgerRepository(remote.Client(base, "secret"))
    for revision in (1, 2, 3):
        repository.hold_revision("inbox", "split", revision)
    repository.hold_revision("relay", "split", 1)

    repository.release_revisions("inbox", "split", 2, 3)
    held = (repository.held_revisions(), stored.held_revisions())
    repository.drop_revisions("relay", "split")

    assert held == ({"inbox": {"split": [1]}, "relay": {"split": [1]}},) * 2
    assert repository.held_revisions() == {"inbox": {"split": [1]}}
    drop = dict(action="drop", kind="inbox", slug="split")
    assert remote.Client(base, "secret").call("POST", "/held", body=drop) == (200, {"ok": True})


def test_artifact_and_media_files_are_stored_listed_and_dropped_through_the_api(
    api: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, base = api
    repository = remote.RemoteLedgerRepository(remote.Client(base, "secret"))
    monkeypatch.setattr(storage, "default", lambda: repository)

    artifact_files.save("split", "a1", b"artifact")
    media_files.save("split", "m1", b"media", 7)

    assert (artifact_files.load("split", "a1"), media_files.load("split", "m1")) == (b"artifact", b"media")
    assert (artifact_files.ids("split"), media_files.ages("split")) == ({"a1"}, {"m1": 7})
    assert artifact_files.load("split", "absent") is None
    artifact_files.drop("split", {"a1"})
    media_files.drop("split", {"m1"})
    assert (artifact_files.ids("split"), media_files.ages("split")) == (set(), {})


def test_the_file_routes_refuse_bad_data_and_paths_past_the_item(api: tuple) -> None:
    _, base = api
    client = remote.Client(base, "secret")

    assert client.call("PUT", "/files/media_files/split/m1", body={"data": "aGk=", "at": 1}) == (200, {"ok": True})
    assert client.call("GET", "/files/media_files/split/m1/extra")[0] == 404
    assert client.call("GET", "/files/media_files/split/absent") == (404, {"code": "not_found"})
    status, answer = client.call("PUT", "/files/artifact_files/split/a1", body={"data": "not base64!"})
    assert (status, answer["code"]) == (400, "bad_request")
    assert client.call("GET", "/files/artifact_files/split") == (200, {})


def test_a_bound_twin_and_a_carried_cache_share_what_the_repository_has_read(api: tuple) -> None:
    _, base = api
    repository = remote.RemoteLedgerRepository(remote.Client(base, "secret"))
    entry = repository.current("split")
    adopted = remote.RemoteLedgerRepository(remote.Client(base, "secret"))

    twin = repository.bound(plain)
    adopted.adopt_carried(repository.carry({}))

    assert repository.bound(repository.domain) is repository
    assert (twin.client, twin.generations()) == (repository.client, repository.generations())
    assert adopted.generations() == {"split": entry.generation}
    assert adopted.current("split").state == entry.state
    assert adopted.export_since("split", 0, lambda fragments: fragments is not None, None) == (entry.generation, True)


def test_the_api_serves_the_ledger_folder_and_relays_its_outbox_unless_told_not_to(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    listening, relayed, served = Listening(), [], []
    monkeypatch.setenv(store.TOKEN_ENV, "secret")
    monkeypatch.delenv(store.PORT_ENV, raising=False)
    monkeypatch.setattr(store.storage, "shared", lambda: None)
    monkeypatch.setattr(store, "repository", lambda: "stored")
    monkeypatch.setattr(store, "server", lambda *given: served.append(given) or listening)
    monkeypatch.setattr(store, "relaying", lambda stored, relay: relayed.append((stored, relay)) or nullcontext())

    assert store.main(["--folder"]) == 0
    assert store.main(["--folder", "--no-relay"]) == 0
    assert served == [("stored", "0.0.0.0", store.DEFAULT_PORT, "secret")] * 2
    assert relayed == [("stored", True), ("stored", False)]
    assert capsys.readouterr().out == "swarm api on 0.0.0.0:4321\n" * 2


def test_the_api_names_the_database_it_needs_when_it_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv(store.TOKEN_ENV, "secret")
    monkeypatch.setattr(store.storage, "shared", lambda: None)
    monkeypatch.setattr(store, "repository", lambda: pytest.fail("the api started without a database"))

    with pytest.raises(SystemExit):
        store.main([])
    assert capsys.readouterr().err.splitlines()[-1] == (
        "agentihooks-swarm-api: error: no shared database URL; pass --folder to serve the ledger folder"
    )


def test_relaying_runs_the_outbox_host_only_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm_ledger import outbox_host

    monkeypatch.setattr(outbox_host, "running", lambda stored: ("running", stored))

    assert store.relaying("stored", True) == ("running", "stored")
    assert isinstance(store.relaying("stored", False), nullcontext)


def test_the_api_announces_its_address_on_a_piped_stdout_while_it_serves(tmp_path: Path) -> None:
    seeded(tmp_path)
    command = [sys.executable, "-m", "scripts.swarm_ledger.api.store", "--open", "--folder", "--no-relay"]
    child = subprocess.Popen(
        [*command, "--host", "127.0.0.1", "--port", "0"],
        cwd=ROOT,
        env=child_env(tmp_path, PYTHONUNBUFFERED="1"),
        stdout=subprocess.PIPE,
        text=True,
    )
    stop = threading.Timer(30, child.kill)
    stop.start()
    try:
        line = child.stdout.readline()
    finally:
        stop.cancel()
        child.terminate()
        child.wait(timeout=10)
        child.stdout.close()

    assert line.startswith("swarm api on 127.0.0.1:")


def test_the_store_serves_a_bound_handler_on_daemon_threads_and_labels_answers_json(tmp_path: Path) -> None:
    listening = store.server(seeded(tmp_path), "127.0.0.1", 0, "")
    listening.server_close()
    with serving(seeded(tmp_path / "labelled"), "") as base:
        data = raw(base, b"GET /healthz HTTP/1.1\r\nHost: api\r\nConnection: close\r\n\r\n")

    assert (listening.RequestHandlerClass.__name__, listening.daemon_threads) == ("Bound", True)
    assert listening.RequestHandlerClass.log_message is store.quiet
    assert data.startswith(b"HTTP/1.1 200")
    assert b"\r\nContent-Type: application/json\r\n" in data


def test_a_body_is_read_up_to_its_limit_and_must_be_an_object(api: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    _, base = api
    client = remote.Client(base, "secret")
    monkeypatch.setattr(store, "MAX_BODY", 2)

    assert client.call("POST", "/held") == (400, {"code": "unknown_action"})
    assert client.call("POST", "/held", body={}) == (400, {"code": "unknown_action"})
    assert client.call("POST", "/held", body=[]) == (400, {"code": "bad_request", "detail": "list"})
    assert client.call("POST", "/held", body={"a": 1}) == (400, {"code": "bad_request", "detail": "8"})


def test_a_bad_request_names_its_cause_and_closes_its_connection(api: tuple) -> None:
    _, base = api
    refused = (
        b"POST /api/v1/store/held HTTP/1.1\r\nHost: api\r\nAuthorization: Bearer secret\r\nContent-Length: 2\r\n\r\n[]"
    )

    data = raw(base, refused + b"GET /healthz HTTP/1.1\r\nHost: api\r\n\r\n")

    assert remote.Client(base, "secret").call("POST", "/held", body={"action": "hold"}) == (
        400,
        {"code": "bad_request", "detail": "'kind'"},
    )
    assert data.startswith(b"HTTP/1.1 400") and data.count(b"HTTP/1.1 ") == 1


def test_a_post_without_a_length_reads_no_body(api: tuple) -> None:
    _, base = api

    data = raw(base, b"POST /api/v1/store/held HTTP/1.1\r\nHost: api\r\nAuthorization: Bearer secret\r\n\r\nX")

    assert data.startswith(b"HTTP/1.1 400") and b'"unknown_action"' in data


def test_a_request_without_credentials_is_refused_whatever_the_token(tmp_path: Path) -> None:
    with serving(seeded(tmp_path), "XXXX") as base:
        assert remote.Client(base).call("GET", "/ledgers") == (401, {"code": "unauthorized"})


def test_the_store_reads_the_token_and_every_event_by_default_and_refuses_a_read_naming_no_part(api: tuple) -> None:
    stored, base = api
    client = remote.Client(base, "secret")

    assert client.call("GET", "/ledgers/split/token") == (200, {"token": stored.token("split")})
    assert client.call("GET", "/ledgers/split/events") == (200, stored.events_since("split", 0))
    assert client.call("GET", "/ledgers/split/read") == (404, {"code": "ledger_not_found"})


def test_the_store_refuses_an_empty_path_an_unknown_ledger_method_and_a_commit_missing_its_drops(api: tuple) -> None:
    stored, base = api
    client = remote.Client(base, "secret")
    revision = stored.current("split").state["_meta"]["rev"]
    body = dict(revision=revision, changed={}, dropped=[], events=[], summary={})

    assert client.call("GET", "/") == (404, {"code": "not_found"})
    assert client.call("PUT", "/ledgers/split", body={}) == (404, {"code": "not_found"})
    assert client.call("POST", "/ledgers/split", body=body) == (404, {"code": "not_found"})
    assert client.call("GET", "/ledgers/split/read/extra") == (404, {"code": "not_found"})
    assert remote.changes({"a": 1, "c": 3}, {"a": 1, "b": 2}) == ({"b": 2}, ["c"])
    with pytest.raises(KeyError), remote.Session(client):
        raise KeyError("inside")
    assert client.call("PUT", "/files/media_files/split/m1", body={"data": "aGk=!", "at": 1})[0] == 400
    for name in body:
        missing = {key: value for key, value in body.items() if key != name}
        assert client.call("POST", "/ledgers/split/commit", body=missing) == (
            400,
            {"code": "bad_request", "fields": [name]},
        )
    status, answer = client.call("POST", "/ledgers/split/commit", body=body)
    assert (status, answer["revision"]) == (200, stored.current("split").state["_meta"]["rev"])


def test_the_client_names_why_a_call_never_reached_the_store() -> None:
    with socket.socket() as spare:
        spare.bind(("127.0.0.1", 0))
        port = spare.getsockname()[1]

    with pytest.raises(remote.Unavailable) as down:
        remote.Client(f"http://127.0.0.1:{port}").call("GET", "/ledgers")
    assert "refused" in str(down.value).lower()


def test_remote_reads_name_the_missing_ledger_and_the_failed_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    repository = offline()
    reads = (
        lambda: repository.read("absent", "title"),
        lambda: repository.events_since("absent", 0),
        lambda: repository.nodes("absent", "children"),
        lambda: repository.nodes("absent", "children", "n1"),
        lambda: repository.current("absent"),
        lambda: repository.token("absent"),
        lambda: repository.commit("absent", 1, {}, {**CONTENT, "_meta": {"rev": 1}}, []),
    )
    monkeypatch.setattr(repository.client, "call", lambda *given, **named: (404, {"code": "ledger_not_found"}))
    for read in reads:
        with pytest.raises(sqlite.Missing) as missing:
            read()
        assert missing.value.args == ("absent",)
    session = remote.Session(repository.client)
    session.saved["bin"] = {}
    monkeypatch.setattr(repository.client, "call", lambda *given, **named: (500, {"code": "store_failed"}))
    for read in (reads[0], reads[4], session.commit):
        with pytest.raises(remote.Unavailable) as failed:
            read()
        assert str(failed.value) == "500 {'code': 'store_failed'}"


def test_a_remote_read_sends_the_generation_it_holds(api: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    _, base = api
    repository = remote.RemoteLedgerRepository(remote.Client(base, "secret"))
    call, sent = repository.client.call, []

    def recorded(method: str, path: str, query: dict | None = None, body: object = None) -> tuple[int, object]:
        sent.append(query)
        return call(method, path, query, body)

    monkeypatch.setattr(repository.client, "call", recorded)
    first = repository.current("split")

    assert repository.current("split") is first
    assert sent == [None, {"read": first.generation}]


def test_remote_hierarchy_reads_ask_for_the_relation_they_name(api: tuple) -> None:
    stored, base = api
    repository = remote.RemoteLedgerRepository(remote.Client(base, "secret"))
    repository.apply_ops("split", ops=[ADD])
    node = stored.nodes("split", "children")[0]["node"]

    assert repository.nodes("split", "subtree") == stored.nodes("split", "subtree")
    assert len(stored.nodes("split", "subtree")) > len(stored.nodes("split", "children"))
    assert repository.nodes("split", "ancestors", node) == stored.nodes("split", "ancestors", node)
    assert stored.nodes("split", "ancestors", node) != stored.nodes("split", "children", node)


def test_remote_rules_run_with_the_gate_on_private_ops_and_drop_unstored_files_of_the_ledger(
    api: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from scripts.swarm_ledger.repository import mutation

    _, base = api
    repository = remote.RemoteLedgerRepository(remote.Client(base, "secret"))
    seen, dropped = [], []

    def apply(slug: str, state: dict, domain: object, changes: list, ops: list, gate: object) -> tuple:
        seen.append((slug, changes, ops, gate))
        return [], SimpleNamespace(changed=False, events=[], unstored={"f1"})

    monkeypatch.setattr(mutation, "apply", apply)
    monkeypatch.setattr(artifact_files, "drop", lambda slug, ids: dropped.append(("artifact", slug, ids)))
    monkeypatch.setattr(media_files, "drop", lambda slug, ids: dropped.append(("media", slug, ids)))
    changes, ops, gate = [{"id": "c1"}], [dict(ADD)], object()

    repository.apply_ops("split", changes, ops=ops, gate=gate)

    ((seen_slug, seen_changes, seen_ops, seen_gate),) = seen
    assert seen_slug == "split" and seen_gate is gate and (seen_changes, seen_ops) == (changes, ops)
    assert seen_changes[0] is not changes[0] and seen_ops[0] is not ops[0]
    assert dropped == [("artifact", "split", {"f1"}), ("media", "split", {"f1"})]


def test_the_remote_repository_keeps_its_domain_document_copies_and_carries_only_moved_ledgers(api: tuple) -> None:
    _, base = api
    repository = remote.RemoteLedgerRepository(remote.Client(base, "secret"), plain)
    document = repository.get_document("split")
    document["title"] = "Changed"

    assert repository.domain is plain and offline().bound(plain).domain is plain
    assert repository.current("split").state["title"] == "Split"
    assert repository.carry(repository.generations()) == {}


def test_a_file_load_the_store_does_not_hold_reads_no_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    session = remote.Session(remote.Client("http://unused"))
    monkeypatch.setattr(session.client, "call", lambda *given, **named: (404, None))

    assert session.execute(media_files.LOAD, ("split", "m1")).fetchall() == []


def test_a_file_drop_answers_ok_and_a_failed_expected_answer_names_its_status(
    api: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, base = api
    client = remote.Client(base, "secret")
    client.call("PUT", "/files/media_files/split/m1", body={"data": "aGk=", "at": 1})

    assert client.call("DELETE", "/files/media_files/split/m1") == (200, {"ok": True})
    assert offline().client.token == ""
    monkeypatch.setattr(client, "call", lambda *given, **named: (500, {"code": "store_failed"}))
    with pytest.raises(remote.Unavailable) as failed:
        client.expect("GET", "/held")
    assert str(failed.value) == "500 {'code': 'store_failed'}"


def test_storage_builds_a_remote_repository_without_a_token_when_none_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTIHOOKS_SWARM_DATABASE_URL", raising=False)
    monkeypatch.delenv(remote.TOKEN_ENV, raising=False)
    monkeypatch.setenv(remote.URL_ENV, "http://api:8766")

    assert storage.build().client.token == ""


def test_a_store_commit_holds_its_ledger_lock_caches_what_it_wrote_and_ignores_absent_drops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "api"
    seeded(folder)
    stored = storage.owned(plain, folder / sqlite.DATABASE)
    revision = stored.current("split").state["_meta"]["rev"]
    summary = stored.summaries()[0]
    lock, locked = plain.LOCK, []

    class Recording:
        def ledger(self, slug: str) -> object:
            locked.append(slug)
            return lock.ledger(slug)

    begin = sqlite.request_metrics.begin_ledger
    monkeypatch.setattr(plain, "LOCK", Recording())
    monkeypatch.setattr(
        sqlite.request_metrics,
        "begin_ledger",
        lambda connection, slug, statement: locked.append(slug) or begin(connection, slug, statement),
    )
    meta = {**stored.current("split").state["_meta"], "rev": revision + 1}
    written = stored.commit("split", revision, {"title": "Moved", "_meta": meta}, ["absent"], [], summary)

    assert "split" in locked and None not in locked
    assert stored.generations() == {"split": written.generation}
    with pytest.raises(sqlite.Conflict) as stale:
        stored.commit("split", revision, {"title": "Stale"}, [], [], summary)
    assert stale.value.slug == "split"


def test_the_api_process_serves_its_own_repository_on_the_plain_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store.storage, "owned", lambda domain: ("owned", domain))

    assert store.repository() == ("owned", plain)


def test_bin_registries_are_read_through_the_api(api: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm_ledger.repository import bin_storage

    stored, base = api
    with stored.connect() as connection, connection:
        stored.save_registry(connection, "bin", {"split": 5})
    monkeypatch.setattr(storage, "default", lambda: remote.RemoteLedgerRepository(remote.Client(base, "secret")))

    assert bin_storage.entries() == {"split": 5}


def test_every_call_to_the_store_waits_at_most_its_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    timeouts = []

    def opened(request: object, timeout: float | None = None) -> object:
        timeouts.append(timeout)
        raise OSError("down")

    monkeypatch.setattr(remote.urllib.request, "urlopen", opened)

    with pytest.raises(remote.Unavailable):
        remote.Client("http://unused").call("GET", "/held")
    assert timeouts == [remote.TIMEOUT_S]


def test_a_registry_session_hands_out_private_copies_of_what_it_read() -> None:
    session = remote.Session(remote.Client("http://unused"))
    session.seen["bin"] = {"split": {"at": 1}}

    session.registry("bin")["split"]["at"] = 2

    assert session.seen["bin"] == {"split": {"at": 1}}


def test_remote_task_rows_carry_the_task_revision_fields_and_commits_cache_by_ledger(api: tuple) -> None:
    _, base = api
    repository = remote.RemoteLedgerRepository(remote.Client(base, "secret"))
    repository.apply_ops("split", ops=[ADD])
    meta = repository.current("split").state["_meta"]

    generation, fields, tasks = repository.task_rows("split")

    assert list(repository.generations()) == ["split"]
    assert fields == {name: meta[name] for name in sqlite.TASK_REVISION_FIELDS if name in meta} and "rev" in fields
    assert (generation, len(tasks)) == (repository.generations()["split"], 2)


def test_a_purge_of_a_ledger_never_read_removes_it(api: tuple) -> None:
    stored, base = api
    repository = remote.RemoteLedgerRepository(remote.Client(base, "secret"))

    with repository.connect() as session:
        repository.purge("split", session)

    assert not stored.exists("split")


def test_storage_builds_the_remote_repository_on_the_domain_it_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTIHOOKS_SWARM_DATABASE_URL", raising=False)
    monkeypatch.setenv(remote.URL_ENV, "http://api:8766")

    assert storage.build(domain=plain).domain is plain
