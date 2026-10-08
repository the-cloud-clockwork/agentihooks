import http.client
import json
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402
import new_ledger  # noqa: E402

from scripts.swarm_ledger import ledger_server as server  # noqa: E402
from scripts.swarm_ledger.events import Hub, stream  # noqa: E402
from scripts.swarm_ledger.events.patch import apply  # noqa: E402
from tests.swarm_ledger import legacy_page  # noqa: E402

SLUG = "stream-2026-01-01"


@pytest.fixture
def live(monkeypatch):
    content = {"title": "Demo", "overview": "o", "sources": [], "phases": [], "questions": [], "followups": []}
    html_path, json_path = core.paths(SLUG)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    core.sync(SLUG)
    monkeypatch.setattr(server, "HUB", Hub())
    monkeypatch.setattr(server, "swarm_status", lambda slug, state=None: {"config": {"state": "running"}})
    monkeypatch.setattr(stream, "HEARTBEAT_S", 0.2)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
    yield httpd.server_address[1], legacy_page.stored_token(html_path)
    httpd.shutdown()
    httpd.server_close()


def connect(port, token, cursor=None, timeout=5):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {"Host": f"127.0.0.1:{server.PORT}", "Accept": "text/event-stream"}
    if token:
        headers["X-Ledger-Token"] = token
    if cursor:
        headers["Last-Event-ID"] = cursor
    conn.request("GET", f"/api/v1/ledgers/{SLUG}/events", headers=headers)
    return conn, conn.getresponse()


def frames(response):
    lines = iter(lambda: response.fp.readline().decode(), "")
    return stream.parse(lines)


def next_of(reader, names):
    for name, data, cursor in reader:
        if name in names:
            return name, data, cursor
    raise AssertionError(f"stream ended before {names}")


def comment(n):
    return {"op": "add", "thread": "notes", "id": f"n-{n}", "text": f"note {n}"}


def test_a_stream_starts_with_a_snapshot_then_sends_only_patches(live):
    port, token = live
    conn, response = connect(port, token)
    assert response.status == 200
    assert response.getheader("Content-Type") == "text/event-stream; charset=utf-8"
    assert response.getheader("Cache-Control") == "no-store"
    reader = frames(response)
    _, snapshot, cursor = next_of(reader, {"snapshot"})
    assert cursor
    assert snapshot["swarm"] == {"config": {"state": "running"}}
    assert set(snapshot) == {"ledger", "swarm"}
    ledger = snapshot["ledger"]
    assert "seeds" not in ledger["_meta"] and "api_operations" not in ledger["_meta"]
    assert ledger["_meta"]["page_version"] == core.page_version()
    assert "crew" in ledger["_meta"]
    server.repository.apply_ops(SLUG, ops=[comment(1)])
    _, change, cursor2 = next_of(reader, {"ledger"})
    assert change["rev"] == ledger["_meta"]["rev"] + 1
    assert set(change["patch"]["o"]) <= {"notes", "_meta", "notifications", "priorities", "alerts"}
    ledger = apply(ledger, change["patch"])
    assert ledger["notes"][-1]["text"] == "note 1"
    assert ledger == server.HUB.resource(SLUG, "ledger")
    assert cursor2 != cursor
    reread = server.ledger_view(server.repository.get_document(SLUG))
    if reread != ledger:
        _, same, _ = next_of(reader, {"ledger"})
        assert same["rev"] == change["rev"]
        ledger = apply(ledger, same["patch"])
    assert ledger == reread
    conn.close()


def test_a_reconnect_replays_the_changes_it_missed_and_nothing_else(live):
    port, token = live
    conn, response = connect(port, token)
    _, snapshot, cursor = next_of(frames(response), {"snapshot"})
    conn.close()
    server.repository.apply_ops(SLUG, ops=[comment(2)])
    server.repository.apply_ops(SLUG, ops=[comment(3)])
    conn, response = connect(port, token, cursor)
    assert response.status == 200
    reader = frames(response)
    first = next_of(reader, {"ledger", "snapshot"})
    second = next_of(reader, {"ledger", "snapshot"})
    assert [first[0], second[0]] == ["ledger", "ledger"]
    ledger = apply(apply(snapshot["ledger"], first[1]["patch"]), second[1]["patch"])
    assert [n["text"] for n in ledger["notes"]][-2:] == ["note 2", "note 3"]
    assert next_of(reader, {"ledger", "heartbeat"})[0] == "heartbeat"
    conn.close()


def test_an_expired_cursor_answers_410_and_a_fresh_connection_reloads(live):
    port, token = live
    conn, response = connect(port, token, "bm90LWEtY3Vyc29y")
    assert response.status == 410
    assert json.loads(response.read())["error"]["code"] == "cursor_expired"
    conn.close()
    conn, response = connect(port, token)
    assert next_of(frames(response), {"snapshot"})[0] == "snapshot"
    conn.close()


def test_a_stream_needs_the_ledger_credential_in_a_header(live):
    port, _ = live
    conn, response = connect(port, None)
    assert response.status == 403
    conn.close()
    assert not server.HUB.has(SLUG)


def test_events_without_the_stream_accept_header_stay_the_paginated_collection(live):
    port, token = live
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(
        "GET", f"/api/v1/ledgers/{SLUG}/events", headers={"Host": f"127.0.0.1:{server.PORT}", "X-Ledger-Token": token}
    )
    response = conn.getresponse()
    assert response.status == 200
    assert response.getheader("Content-Type") == "application/json"
    assert "next_cursor" in json.loads(response.read())
    conn.close()
    assert not server.HUB.has(SLUG)


def test_an_idle_stream_sends_heartbeats(live):
    port, token = live
    conn, response = connect(port, token)
    reader = frames(response)
    next_of(reader, {"snapshot"})
    assert next_of(reader, {"heartbeat", "ledger"})[0] == "heartbeat"
    conn.close()


def test_a_closed_stream_unsubscribes_and_stops_sampling(live):
    port, token = live
    conn, response = connect(port, token)
    next_of(frames(response), {"snapshot"})
    assert server.HUB.watched() == [SLUG]
    response.close()
    conn.close()
    for _ in range(100):
        if not server.HUB.watched():
            break
        time.sleep(0.05)
    assert server.HUB.watched() == []
    assert server.HUB.has(SLUG)


def test_sampling_publishes_swarm_changes_only_for_watched_ledgers(live, monkeypatch):
    port, token = live
    conn, response = connect(port, token)
    reader = frames(response)
    next_of(reader, {"snapshot"})
    monkeypatch.setattr(server, "swarm_status", lambda slug, state=None: {"config": {"state": "paused"}})
    seen = []
    monkeypatch.setattr(server.HUB, "evict", lambda: seen.append("evict"))
    server.sample_streams()
    assert seen == ["evict"]
    name, change, _ = next_of(reader, {"swarm"})
    assert change == {"patch": {"o": {"config": {"o": {"state": {"v": "paused"}}}}}}
    conn.close()


def test_two_tabs_share_one_channel_and_both_receive_a_change(live):
    port, token = live
    one, first = connect(port, token)
    two, second = connect(port, token)
    readers = [frames(first), frames(second)]
    for reader in readers:
        next_of(reader, {"snapshot"})
    server.repository.apply_ops(SLUG, ops=[comment(4)])
    assert [next_of(reader, {"ledger"})[1]["rev"] for reader in readers] == [
        server.HUB.resource(SLUG, "ledger")["_meta"]["rev"]
    ] * 2
    one.close()
    two.close()
