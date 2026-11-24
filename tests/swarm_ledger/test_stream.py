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
    html_path, json_path = legacy_page.paths(SLUG, core)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    core.sync(SLUG)
    monkeypatch.setattr(server, "HUB", Hub())
    monkeypatch.setattr(server, "swarm_status", lambda slug, state=None: {"config": {"state": "running"}})
    monkeypatch.setattr(stream, "HEARTBEAT_S", 0.2)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = False
    threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
    yield httpd.server_address[1], legacy_page.stored_token(html_path)
    httpd.shutdown()
    server.HUB.drain()
    httpd.server_close()
    assert server.HUB.watched() == []


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


def next_of(reader, names, timeout=5):
    deadline = time.monotonic() + timeout
    for name, data, cursor in reader:
        if time.monotonic() >= deadline:
            raise AssertionError(f"stream timed out waiting for {names}")
        if name in names:
            return name, data, cursor
    raise AssertionError(f"stream ended before {names}")


def test_heartbeats_do_not_extend_the_next_event_deadline(monkeypatch):
    from types import SimpleNamespace

    ticks = iter([0, 0.25, 1])
    monkeypatch.setattr(sys.modules[__name__], "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    reader = iter([("heartbeat", {}, None), ("heartbeat", {}, None), ("ledger", {}, "late")])
    with pytest.raises(AssertionError, match="timed out.*ledger"):
        next_of(reader, {"ledger"}, timeout=1)
    assert next(reader) == ("ledger", {}, "late")


def comment(n):
    return {"op": "add", "thread": "notes", "id": f"n-{n}", "text": f"note {n}"}


@pytest.mark.feature("ledger-stream-no-snapshot-connect")
def test_a_cursorless_connect_opens_without_building_the_page_view(live, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a stream connect built the whole ledger page view")

    monkeypatch.setattr(server, "page_view", refuse)
    port, token = live
    conn, response = connect(port, token)
    assert response.status == 200
    name, data, cursor = next(iter(frames(response)))
    assert name == "open" and cursor
    assert "ledger" not in data and data["swarm"] == {"config": {"state": "running"}}
    conn.close()


def test_a_stream_opens_without_a_ledger_then_announces_it_and_sends_patches(live):
    port, token = live
    conn, response = connect(port, token)
    assert response.status == 200
    assert response.getheader("Content-Type") == "text/event-stream; charset=utf-8"
    assert response.getheader("Cache-Control") == "no-store"
    reader = frames(response)
    _, first, cursor = next_of(reader, {"open"})
    assert cursor
    assert first == {"rev": None, "swarm": {"config": {"state": "running"}}}
    server.repository.apply_ops(SLUG, ops=[comment(1)])
    _, born, cursor2 = next_of(reader, {"open"})
    assert cursor2 != cursor
    ledger = server.HUB.resource(SLUG, "ledger")
    assert born == {"rev": ledger["_meta"]["rev"]}
    assert ledger["notes"][-1]["text"] == "note 1"
    server.repository.apply_ops(SLUG, ops=[comment(2)])
    _, change, cursor3 = next_of(reader, {"ledger"})
    assert change["rev"] == ledger["_meta"]["rev"] + 1
    assert set(change["patch"]["o"]) <= {"notes", "_meta", "notifications", "priorities", "alerts"}
    ledger = apply(ledger, change["patch"])
    assert ledger["notes"][-1]["text"] == "note 2"
    assert ledger == server.HUB.resource(SLUG, "ledger")
    assert cursor3 != cursor2
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
    _, _, cursor = next_of(frames(response), {"open"})
    conn.close()
    server.repository.apply_ops(SLUG, ops=[comment(2)])
    server.repository.drain(5)
    baseline = server.HUB.resource(SLUG, "ledger")
    server.repository.apply_ops(SLUG, ops=[comment(3)])
    server.repository.drain(5)
    conn, response = connect(port, token, cursor)
    assert response.status == 200
    reader = frames(response)
    first = next_of(reader, {"ledger", "open"})
    second = next_of(reader, {"ledger", "open"})
    assert [first[0], second[0]] == ["open", "ledger"]
    assert first[1]["rev"] == baseline["_meta"]["rev"]
    ledger = apply(baseline, second[1]["patch"])
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
    assert next_of(frames(response), {"open"})[0] == "open"
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
    next_of(reader, {"open"})
    assert next_of(reader, {"heartbeat", "ledger"})[0] == "heartbeat"
    conn.close()


def test_a_closed_stream_unsubscribes_and_stops_sampling(live):
    port, token = live
    conn, response = connect(port, token)
    next_of(frames(response), {"open"})
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
    next_of(reader, {"open"})
    server.repository.apply_ops(SLUG, ops=[comment(1)])
    next_of(reader, {"open"})
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
        next_of(reader, {"open"})
    server.repository.apply_ops(SLUG, ops=[comment(4)])
    for reader in readers:
        next_of(reader, {"open"})
    server.repository.apply_ops(SLUG, ops=[comment(5)])
    assert [next_of(reader, {"ledger"})[1]["rev"] for reader in readers] == [
        server.HUB.resource(SLUG, "ledger")["_meta"]["rev"]
    ] * 2
    one.close()
    two.close()
