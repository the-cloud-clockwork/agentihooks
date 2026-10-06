import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))
import ledger_core as core  # noqa: E402
import ledger_layout as layout  # noqa: E402
import ledger_server as server  # noqa: E402

SAVED = {
    "capacity-box": {"height": 120},
    "swarm-row-work": {"height": 320, "split": 62.5},
    "swarm-row-accounts": {"split": 40},
    "health-box": {"height": 200},
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


def call(port, method="GET", body=None, origin=f"http://127.0.0.1:{server.PORT}", ctype="application/json"):
    headers = {"Host": f"127.0.0.1:{server.PORT}", "Content-Type": ctype}
    if origin:
        headers["Origin"] = origin
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/layout", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def test_the_layout_lives_in_one_file_in_the_ledger_folder():
    assert layout.path() == core.LEDGER_DIR / ".swarm-layout"


def test_no_saved_layout_reads_as_the_defaults():
    assert layout.read() == {}


def test_a_written_layout_reads_back_for_every_ledger():
    assert layout.write(SAVED) == SAVED
    assert layout.read() == SAVED


def test_sizes_are_rounded_to_whole_pixels_and_tenths_of_a_percent():
    assert layout.write({"swarm-row-work": {"height": 320.6, "split": 62.44}}) == {
        "swarm-row-work": {"height": 321, "split": 62.4}
    }


def test_an_empty_layout_resets_to_the_defaults():
    layout.write(SAVED)
    assert layout.write({}) == {}
    assert layout.read() == {}


def test_an_unreadable_file_reads_as_the_defaults():
    layout.path().write_text("{not json", encoding="utf-8")
    assert layout.read() == {}
    layout.path().write_text(json.dumps({"nowhere": {"height": 100}}), encoding="utf-8")
    assert layout.read() == {}


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ([], "layout must be an object of rows"),
        ({"nowhere": {"height": 100}}, "unknown row nowhere"),
        ({"health-box": 100}, "health-box must be an object of sizes"),
        ({"health-box": {}}, "health-box must be an object of sizes"),
        ({"health-box": {"split": 50}}, "health-box takes only height"),
        ({"swarm-row-work": {"width": 50}}, "swarm-row-work takes only height, split"),
        ({"health-box": {"height": 47}}, "health-box height must be a number from 48 to 4000"),
        ({"health-box": {"height": 4001}}, "health-box height must be a number from 48 to 4000"),
        ({"health-box": {"height": True}}, "health-box height must be a number from 48 to 4000"),
        ({"health-box": {"height": "100"}}, "health-box height must be a number from 48 to 4000"),
        ({"swarm-row-work": {"split": 14.9}}, "swarm-row-work split must be a number from 15 to 85"),
        ({"swarm-row-work": {"split": 85.1}}, "swarm-row-work split must be a number from 15 to 85"),
    ],
)
def test_a_layout_out_of_shape_is_refused(body, message):
    with pytest.raises(ValueError) as error:
        layout.write(body)
    assert str(error.value) == message
    assert not layout.path().exists()


@pytest.mark.parametrize(("key", "value"), [("height", 48), ("height", 4000), ("split", 15), ("split", 85)])
def test_the_size_limits_are_inclusive(key, value):
    assert layout.write({"swarm-row-work": {key: value}}) == {"swarm-row-work": {key: value}}


def test_the_route_reads_the_defaults_then_the_saved_layout(port):
    assert call(port) == (200, "{}")
    assert call(port, "PUT", SAVED) == (200, json.dumps(SAVED))
    assert json.loads(call(port)[1]) == SAVED
    assert call(port, "PUT", {}) == (200, "{}")
    assert call(port) == (200, "{}")


def test_the_route_refuses_a_bad_layout_and_keeps_the_saved_one(port):
    call(port, "PUT", SAVED)
    assert call(port, "PUT", {"nowhere": {"height": 100}}) == (400, "unknown row nowhere")
    assert call(port, "PUT", b"{oops") == (400, "body is not JSON")
    assert json.loads(call(port)[1]) == SAVED


def test_the_route_refuses_writes_from_another_origin_or_without_json(port):
    assert call(port, "PUT", SAVED, origin="http://evil.test") == (403, "origin not allowed")
    assert call(port, "PUT", SAVED, origin=None) == (403, "origin not allowed")
    assert call(port, "PUT", SAVED, ctype="text/plain") == (415, "Content-Type must be application/json")
    assert call(port) == (200, "{}")
