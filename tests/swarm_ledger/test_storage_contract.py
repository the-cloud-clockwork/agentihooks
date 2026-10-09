import http.client
import json
import sys
import threading
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))

import ledger_core as core  # noqa: E402

from scripts.swarm_ledger import ledger_server as server  # noqa: E402
from scripts.swarm_ledger.repository import repository  # noqa: E402
from scripts.swarm_ledger.repository.sqlite import DATABASE  # noqa: E402
from tests.swarm_ledger.test_sqlite import CONTENT, store  # noqa: E402

pytestmark = pytest.mark.xdist_group("fakeredis")


def ledger_files(folder, slug):
    return sorted(p.name for p in Path(folder).iterdir() if p.stem == slug)


@pytest.fixture(scope="module")
def live():
    slug = f"contract-{uuid.uuid4().hex[:8]}"
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    assert repository.create(slug, {"title": "Contract", "phases": [{"title": "One", "description": "d"}]})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    host = f"127.0.0.1:{httpd.server_address[1]}"
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    with (
        patch("hooks._redis.get_redis", return_value=None),
        patch("scripts.gates.talk.Budget._marks", return_value=None),
        patch.object(server, "relay_to_inbox", return_value=None),
        patch.object(server, "doctor_phrase", return_value=None),
        patch.object(server, "ALLOWED_HOSTS", {host}),
    ):
        thread.start()
        try:
            yield {"slug": slug, "port": httpd.server_address[1], "host": host, "token": repository.token(slug)}
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)


def send(live, method, path, body=b"", **headers):
    conn = http.client.HTTPConnection("127.0.0.1", live["port"], timeout=5)
    try:
        conn.request(method, path, body, {"Host": live["host"], **headers})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def save(live, text="hello"):
    op = {"op": "add", "id": uuid.uuid4().hex, "thread": "notes", "text": text}
    headers = {"Content-Type": "application/json", "X-Ledger-Token": live["token"]}
    status, body = send(live, "PUT", f"/api/{live['slug']}", json.dumps({"ops": [op]}), **headers)
    assert status == 200, body
    return op["id"], json.loads(body)


def test_the_whole_document_read_answers_gone_and_names_the_versioned_route(live):
    status, body = send(live, "GET", f"/api/{live['slug']}", **{"X-Ledger-Token": live["token"]})
    assert status == 410
    assert body.decode() == f"read the ledger from /api/v1/ledgers/{live['slug']}"


def test_a_page_save_answers_a_bounded_acknowledgment_without_the_ledger(live):
    op_id, reply = save(live)
    assert set(reply) == {"applied", "rejected", "_meta"}
    assert reply["applied"] == [op_id] and reply["rejected"] == []
    assert set(reply["_meta"]) == {"rev", "warnings"}


def test_the_acknowledged_revision_is_the_stored_revision(live):
    _, reply = save(live)
    assert reply["_meta"]["rev"] == repository.get_document(live["slug"])["_meta"]["rev"]


def test_each_page_save_advances_the_stored_revision(live):
    _, first = save(live)
    _, second = save(live)
    assert second["_meta"]["rev"] > first["_meta"]["rev"]


def test_a_page_save_writes_no_ledger_file(live):
    before = ledger_files(core.LEDGER_DIR, live["slug"])
    save(live)
    assert ledger_files(core.LEDGER_DIR, live["slug"]) == before == []


def test_serving_the_page_shell_writes_nothing_and_keeps_the_revision(live):
    rev = repository.get_document(live["slug"])["_meta"]["rev"]
    status, body = send(live, "GET", f"/{live['slug']}")
    assert status == 200 and b"<html" in body.lower()
    assert ledger_files(core.LEDGER_DIR, live["slug"]) == []
    assert repository.get_document(live["slug"])["_meta"]["rev"] == rev


def reply_of(state, rejected=(), ops=None, refusals=None):
    handler = object.__new__(server.Handler)
    sent = {}
    handler.send = lambda status, body, kind: sent.update(status=status, body=json.loads(body), kind=kind)
    fake = SimpleNamespace(apply_ops=lambda *args, **kwargs: (state, list(rejected)))
    relayed = []
    with (
        patch.object(server, "repository", fake),
        patch.object(server, "relay_to_inbox", lambda slug, state: relayed.append(slug)),
        patch.object(server, "doctor_phrase", lambda *args: None),
        patch.object(server, "deliver_alerts", lambda *args: None),
        patch("scripts.gates.talk.Budget", lambda slug: None),
    ):
        handler.reply_state("ledger", None, ops, refusals)
    return sent, relayed


def test_the_save_reply_keeps_at_most_twenty_warnings():
    sent, _ = reply_of({"_meta": {"rev": 3, "warnings": [f"w{i}" for i in range(25)]}})
    assert sent["status"] == 200 and sent["kind"] == "application/json"
    assert sent["body"]["_meta"] == {"rev": 3, "warnings": [f"w{i}" for i in range(20)]}


def test_each_save_warning_is_cut_to_a_thousand_characters():
    sent, _ = reply_of({"_meta": {"rev": 1, "warnings": ["x" * 1500, "short"]}})
    assert sent["body"]["_meta"]["warnings"] == ["x" * 1000, "short"]


def test_rejected_and_refused_operations_are_listed_and_never_reported_applied():
    ops = [{"id": "kept"}, {"id": "gated"}]
    sent, _ = reply_of({"_meta": {"rev": 2}}, rejected=["gated"], ops=ops, refusals={"refused": "no"})
    assert sent["body"]["applied"] == ["kept"]
    assert sent["body"]["rejected"] == ["gated", "refused"]
    assert sent["body"]["_meta"]["warnings"] == ["no"]


def test_a_save_without_changes_or_operations_relays_nothing():
    _, relayed = reply_of({"_meta": {"rev": 1}})
    assert relayed == []
    _, relayed = reply_of({"_meta": {"rev": 2}}, ops=[{"id": "one"}])
    assert relayed == ["ledger"]


def test_creating_a_ledger_writes_only_the_database(tmp_path):
    repo = store(tmp_path)
    assert repo.create("ledger", CONTENT)
    names = {p.name for p in (tmp_path / "ledgers").iterdir()}
    assert DATABASE in names
    assert not {name for name in names if name.endswith((".json", ".html"))}


def test_bin_delete_and_restore_write_no_ledger_file_and_keep_the_document(tmp_path):
    repo = store(tmp_path)
    repo.create("ledger", CONTENT)
    before = repo.export_document("ledger")
    repo.delete("ledger", now=1000)
    assert repo.restore("ledger", now=2000)
    assert ledger_files(tmp_path / "ledgers", "ledger") == []
    assert repo.export_document("ledger")["tasks"] == before["tasks"]


def test_a_fresh_repository_on_the_folder_reads_the_same_ledger_and_token(tmp_path):
    repo = store(tmp_path)
    repo.create("ledger", CONTENT)
    repo.apply_ops("ledger", ops=[{"op": "join", "id": "j1", "by": "eng"}])
    again = store(tmp_path)
    assert again.export_document("ledger") == repo.export_document("ledger")
    assert again.token("ledger") == repo.token("ledger")
