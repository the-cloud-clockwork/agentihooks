import http.client
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from scripts.swarm_ledger import ledger_layout as layout
from scripts.swarm_ledger import ledger_server as server

core = layout.core

SAVED = {
    "capacity-box": {"height": 120},
    "quota-box": {"height": 220},
    "overlays-box": {"height": 160},
    "agents-box": {"height": 320},
    "hives-box": {"height": 200},
    "handoff-box": {"height": 180},
}


@pytest.fixture(autouse=True)
def fresh():
    layout.path().unlink(missing_ok=True)
    yield
    layout.path().unlink(missing_ok=True)


@pytest.fixture(scope="module")
def port():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def send(port, method="GET", body=None, origin=f"http://127.0.0.1:{server.PORT}", ctype="application/json", query=""):
    headers = {"Host": f"127.0.0.1:{server.PORT}", "Content-Type": ctype}
    if origin:
        headers["Origin"] = origin
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    url = f"http://127.0.0.1:{port}/api/layout{query}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode(), resp.headers.get("Content-Type")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(), exc.headers.get("Content-Type")


def call(port, method="GET", body=None, **options):
    return send(port, method, body, **options)[:2]


def test_the_route_answers_json_and_ignores_a_query(port):
    assert send(port, query="?v=1") == (200, "{}", "application/json")
    assert send(port, "PUT", SAVED, query="?v=1") == (200, json.dumps(SAVED), "application/json")
    assert call(port, query="?v=2") == (200, json.dumps(SAVED))


def test_the_route_takes_a_body_up_to_the_size_limit(port):
    assert call(port, "PUT", b"{}" + b" " * (server.MAX_BODY - 2)) == (200, "{}")
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.putrequest("PUT", "/api/layout", skip_host=True)
    for name, value in (
        ("Host", f"127.0.0.1:{server.PORT}"),
        ("Origin", f"http://127.0.0.1:{server.PORT}"),
        ("Content-Type", "application/json"),
        ("Content-Length", str(server.MAX_BODY + 1)),
    ):
        conn.putheader(name, value)
    conn.endheaders()
    reply = conn.getresponse()
    assert (reply.status, reply.read().decode()) == (400, "body size out of range")
    conn.close()


def test_the_layout_lives_in_one_file_in_the_ledger_folder():
    assert layout.path() == core.LEDGER_DIR / ".swarm-layout"


def test_no_saved_layout_reads_as_the_defaults():
    assert layout.read() == {}


def test_a_written_layout_reads_back_for_every_ledger():
    assert layout.write(SAVED) == SAVED
    assert layout.read() == SAVED


def test_sizes_are_rounded_to_whole_pixels():
    assert layout.write({"agents-box": {"height": 320.6}}) == {"agents-box": {"height": 321}}


def test_an_empty_layout_resets_to_the_defaults():
    layout.write(SAVED)
    assert layout.write({}) == {}
    assert layout.read() == {}


def test_an_empty_body_reads_as_the_defaults_and_a_broken_one_is_refused():
    assert layout.loads(b"") == {}
    assert layout.loads(b'{"handoff-box": {"height": 90}}') == {"handoff-box": {"height": 90}}
    with pytest.raises(ValueError) as error:
        layout.loads(b"{oops")
    assert str(error.value) == "body is not JSON"


def test_an_unreadable_file_reads_as_the_defaults():
    layout.path().write_text("{not json", encoding="utf-8")
    assert layout.read() == {}
    layout.path().write_text(json.dumps({"nowhere": {"height": 100}}), encoding="utf-8")
    assert layout.read() == {}
    saved = {"health-box": {"height": 100}, "agents-box": {"height": 200}}
    layout.path().write_text(json.dumps(saved), encoding="utf-8")
    assert layout.read() == {"agents-box": {"height": 200}}


def test_the_layout_rows_carry_no_health_row():
    assert "health-box" not in layout.ROWS
    assert {"quota-box", "hives-box", "agents-box", "handoff-box", "recovery-box"} <= set(layout.ROWS)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ([], "layout must be an object of rows"),
        ({"nowhere": {"height": 100}}, "unknown row nowhere"),
        ({"health-box": {"height": 100}}, "unknown row health-box"),
        ({"handoff-box": 100}, "handoff-box must be an object of sizes"),
        ({"handoff-box": {}}, "handoff-box must be an object of sizes"),
        ({"handoff-box": {"split": 50}}, "handoff-box takes only height"),
        ({"quota-box": {"width": 50}}, "quota-box takes only height"),
        ({"handoff-box": {"height": 47}}, "handoff-box height must be a number from 48 to 4000"),
        ({"handoff-box": {"height": 4001}}, "handoff-box height must be a number from 48 to 4000"),
        ({"handoff-box": {"height": True}}, "handoff-box height must be a number from 48 to 4000"),
        ({"handoff-box": {"height": "100"}}, "handoff-box height must be a number from 48 to 4000"),
    ],
)
def test_a_layout_out_of_shape_is_refused(body, message):
    with pytest.raises(ValueError) as error:
        layout.write(body)
    assert str(error.value) == message
    assert not layout.path().exists()


@pytest.mark.parametrize("value", [48, 4000])
def test_the_size_limits_are_inclusive(value):
    assert layout.write({"quota-box": {"height": value}}) == {"quota-box": {"height": value}}


def test_read_drops_an_unknown_saved_row_and_keeps_the_known_ones():
    layout.path().write_text(
        json.dumps({"swarm-row-work": {"height": 320}, "quota-box": {"height": 200}}), encoding="utf-8"
    )
    assert layout.read() == {"quota-box": {"height": 200}}


def test_the_route_reads_the_defaults_then_the_saved_layout(port):
    assert call(port) == (200, "{}")
    assert call(port, "PUT", SAVED) == (200, json.dumps(SAVED))
    assert json.loads(call(port)[1]) == SAVED
    assert call(port, "PUT", {}) == (200, "{}")
    assert call(port) == (200, "{}")
    call(port, "PUT", SAVED)
    assert call(port, "PUT", b"") == (200, "{}")
    assert call(port) == (200, "{}")


def test_the_route_refuses_a_bad_layout_and_keeps_the_saved_one(port):
    call(port, "PUT", SAVED)
    assert send(port, "PUT", {"nowhere": {"height": 100}}) == (400, "unknown row nowhere", "text/plain")
    assert send(port, "PUT", b"{oops") == (400, "body is not JSON", "text/plain")
    assert json.loads(call(port)[1]) == SAVED


def test_the_route_refuses_writes_from_another_origin_or_without_json(port):
    assert send(port, "PUT", SAVED, origin="http://evil.test") == (403, "origin not allowed", "text/plain")
    assert send(port, "PUT", SAVED, origin=None) == (403, "origin not allowed", "text/plain")
    assert send(port, "PUT", SAVED, ctype="text/plain") == (
        415,
        "Content-Type must be application/json",
        "text/plain",
    )
    assert call(port) == (200, "{}")


def test_a_write_without_a_content_length_resets_at_once(port):
    call(port, "PUT", SAVED)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    conn.putrequest("PUT", "/api/layout", skip_host=True, skip_accept_encoding=True)
    conn.putheader("Host", f"127.0.0.1:{server.PORT}")
    conn.putheader("Origin", f"http://127.0.0.1:{server.PORT}")
    conn.putheader("Content-Type", "application/json")
    conn.endheaders()
    reply = conn.getresponse()
    assert (reply.status, reply.read().decode()) == (200, "{}")
    conn.close()
    assert call(port) == (200, "{}")


def test_only_the_path_before_the_first_question_mark_names_the_route(port):
    assert call(port, "PUT", SAVED, query="?a?b") == (200, json.dumps(SAVED))
