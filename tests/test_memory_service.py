"""Tests for the hosted session memory service: bearer authentication, routes, configuration and its entrypoint."""

import hashlib
import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path

import pytest

from hooks.memory import service
from hooks.memory.api import records
from hooks.memory.api import search as search_api
from hooks.memory.api.ingest import SCHEMA_VERSION, Grant
from tests import sv2_idx05_cases
from tests.sv2_idx05_cases import Bank, body, grant

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
TOKEN, NARROW_TOKEN = "fixture-token", "fixture-narrow-token"


def hashed(token):
    return hashlib.sha256(token.encode()).hexdigest()


@pytest.fixture
def inputs():
    return json.loads(sv2_idx05_cases.FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def bank(inputs, tmp_path):
    return Bank(inputs, tmp_path)


@pytest.fixture
def grants(inputs):
    return {hashed(TOKEN): grant(inputs), hashed(NARROW_TOKEN): grant(inputs, narrow=True)}


@pytest.fixture
def served(bank, grants):
    server = HTTPServer(("127.0.0.1", 0), service.handler(bank.tools, grants))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def call(base, path, payload=None, token=None, header=None, raw=None, method="POST"):
    data = raw if raw is not None else (None if payload is None else json.dumps(payload).encode())
    request = urllib.request.Request(base + path, data=data, method=method)
    if header is not None:
        request.add_header("Authorization", header)
    elif token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, dict(response.headers), json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), json.loads(error.read())


def refusal(error_class, detail):
    return {
        "schema_version": SCHEMA_VERSION,
        "state": "rejected",
        "error": {"class": error_class, "retry": "never", "detail": detail},
    }


@pytest.mark.parametrize(
    "header",
    [None, "", "Bearer", "Bearer ", "Basic Zml4dHVyZS10b2tlbg==", "bearer fixture-token", "Bearer wrong-token"],
)
@pytest.mark.parametrize("path", [service.SEARCH_PATH, service.TOOLS_PREFIX + "session_list"])
def test_a_missing_or_unknown_token_is_refused_before_any_catalog_read(inputs, bank, served, header, path):
    before, rejections = bank.protected(), bank.counter("session_search_scope_rejections")
    payload = body(inputs, query=inputs["queries"]["hit"]) if path == service.SEARCH_PATH else body(inputs)
    status, headers, answer = call(served, path, payload, header=header)
    assert status == 401 and headers["WWW-Authenticate"] == "Bearer"
    assert answer == refusal("unauthenticated", service.UNAUTHENTICATED)
    assert bank.protected() == before and bank.counter("session_search_scope_rejections") == rejections


def test_a_valid_token_lists_with_the_grant_from_the_token_file(inputs, bank, served):
    status, headers, answer = call(served, service.TOOLS_PREFIX + "session_list", body(inputs, limit=2), TOKEN)
    expected = records.call(bank.tools, "session_list", grant(inputs), body(inputs, limit=2), time.time())
    assert status == 200 and headers["Content-Type"] == "application/json" and answer == expected
    assert [bank.short(item["session_uid"]) for item in answer["sessions"]] == ["lock", "kernel"]


def test_search_answers_as_the_search_call(inputs, bank, served):
    request = body(inputs, query=inputs["queries"]["hit"], context=0)
    status, _, answer = call(served, service.SEARCH_PATH, request, TOKEN)
    assert status == 200 and answer == search_api.search(bank.tools.searcher, grant(inputs), request)
    assert answer["state"] == "answered" and answer["results"]


def ingest_body(inputs):
    return {
        "schema_version": SCHEMA_VERSION,
        "batch_id": "http-ingest",
        "access_domain": inputs["grant"]["corpus"],
        "session_uid": "http-session",
        "source_generation": "http-source",
        "events": [
            {
                "sequence": 1,
                "byte_start": 0,
                "byte_end": 10,
                "project_id": inputs["grant"]["projects"][0],
                "record": {"kind": "user_text", "text": "persisted over HTTP"},
            }
        ],
    }


def test_http_ingest_persists_and_replays_without_claiming_indexing(served, bank, inputs):
    payload = ingest_body(inputs)
    status, _, result = call(served, "/v2/sessions/ingest", payload, TOKEN)
    assert status == 200
    assert result["state"] == "committed"
    assert result["durable_through"] == 1
    assert result["indexed"] is False
    events = bank.tools.catalog.events(payload["access_domain"], payload["session_uid"], payload["source_generation"])
    assert len(events) == 1
    assert events[0]["record"]["text"] == "persisted over HTTP"
    status, _, replay = call(served, "/v2/sessions/ingest", payload, TOKEN)
    assert status == 200
    assert replay["state"] == "replayed"
    assert replay["accepted"] == result["accepted"]
    assert replay["indexed"] is False


@pytest.mark.parametrize("token", [None, "unknown"])
def test_http_ingest_authenticates_before_reading_the_body(served, bank, token, monkeypatch):
    def unread(_):
        raise AssertionError("unauthenticated request reached body parsing")

    monkeypatch.setattr(service, "read_body", unread)
    before = bank.tools.catalog.snapshot()
    status, headers, result = call(served, "/v2/sessions/ingest", {}, token)
    assert status == 401
    assert headers["WWW-Authenticate"] == "Bearer"
    assert result["error"]["class"] == "unauthenticated"
    assert bank.tools.catalog.snapshot() == before


@pytest.mark.parametrize("outside", ["corpus", "project"])
def test_http_ingest_refuses_scope_widening(served, bank, inputs, outside):
    payload = ingest_body(inputs)
    if outside == "corpus":
        payload["access_domain"] = "other-corpus"
    else:
        payload["events"][0]["project_id"] = "other-project"
    before = bank.tools.catalog.snapshot()
    status, _, result = call(served, "/v2/sessions/ingest", payload, TOKEN)
    assert status == 200
    assert result["error"]["class"] == "forbidden_scope"
    assert bank.tools.catalog.snapshot() == before


def test_http_ingest_reuses_bounded_json_validation(served):
    status, _, result = call(served, "/v2/sessions/ingest", raw=b"[]", token=TOKEN)
    assert status == 400
    assert result["error"]["class"] == "invalid_request"


def test_the_narrow_token_sees_only_its_projects(inputs, served):
    _, _, wide = call(served, service.TOOLS_PREFIX + "session_list", body(inputs), TOKEN)
    _, _, narrow = call(served, service.TOOLS_PREFIX + "session_list", body(inputs), NARROW_TOKEN)
    assert narrow["state"] == "answered" and narrow["total"] < wide["total"]


def test_a_caller_named_corpus_never_widens_the_grant(inputs, bank, served):
    request = {**body(inputs), "access_domain": inputs["foreign_corpus"]}
    status, _, answer = call(served, service.TOOLS_PREFIX + "session_list", request, NARROW_TOKEN)
    assert status == 200 and answer["state"] == "rejected" and answer["error"]["class"] == "forbidden_scope"


def test_an_unknown_tool_name_is_the_tool_refusal(inputs, served):
    status, _, answer = call(served, service.TOOLS_PREFIX + "session_drop", body(inputs), TOKEN)
    assert status == 200 and answer["error"]["detail"] == records.UNKNOWN_TOOL


@pytest.mark.parametrize("raw", [b"{not json", b"[1, 2]", b'"text"'])
def test_a_body_that_is_not_a_json_object_is_refused(served, raw):
    status, _, answer = call(served, service.SEARCH_PATH, token=TOKEN, raw=raw)
    assert status == 400 and answer == refusal("invalid_request", service.BAD_BODY)


def test_an_empty_body_is_refused(served):
    status, _, answer = call(served, service.SEARCH_PATH, token=TOKEN, raw=b"")
    assert status == 400 and answer == refusal("invalid_request", service.BAD_BODY)


def test_a_body_over_the_bound_is_refused(served):
    raw = json.dumps({"query": "x" * service.MAX_BODY}).encode()
    status, _, answer = call(served, service.SEARCH_PATH, token=TOKEN, raw=raw)
    assert status == 400 and answer == refusal("invalid_request", service.BAD_BODY)


class Unread:
    def read(self, length):
        raise AssertionError(f"read {length} bytes of a refused body")


class Posted:
    def __init__(self, length, rfile):
        self.headers = {} if length is None else {"Content-Length": str(length)}
        self.rfile = rfile


@pytest.mark.parametrize("length", [None, 0, 1, service.MAX_BODY + 1])
def test_a_body_outside_the_bound_is_refused_without_reading(length):
    assert service.read_body(Posted(length, Unread())) is None


def test_the_smallest_and_the_largest_object_bodies_are_read():
    assert service.read_body(Posted(2, io.BytesIO(b"{}"))) == {}
    largest = b'{"q": "' + b"x" * (service.MAX_BODY - 9) + b'"}'
    assert len(largest) == service.MAX_BODY
    assert service.read_body(Posted(service.MAX_BODY, io.BytesIO(largest))) == {"q": "x" * (service.MAX_BODY - 9)}


def test_an_answer_states_its_length(served):
    with urllib.request.urlopen(served + service.HEALTH_PATH, timeout=10) as response:
        raw = response.read()
    assert response.headers["Content-Length"] == str(len(raw)) and json.loads(raw) == {"state": "ready"}


def test_the_handler_is_quiet_and_named_for_sessions():
    built = service.handler(None, {})
    assert built.__name__ == "Sessions" and built.log_message is service.quiet


@pytest.mark.parametrize(
    "method,path", [("POST", "/v2/sessions/ingest"), ("POST", "/"), ("GET", "/v2/sessions/search")]
)
def test_unknown_routes_are_not_found(served, method, path):
    status, _, answer = call(served, path, {} if method == "POST" else None, TOKEN, method=method)
    assert status == 404 and answer == refusal("not_found", service.NOT_FOUND)


def test_health_answers_ready_without_a_token(served):
    status, _, answer = call(served, service.HEALTH_PATH, method="GET")
    assert (status, answer) == (200, {"state": "ready"})


class Down:
    def execute(self, *args):
        raise sqlite3.OperationalError("database is locked")


def test_health_reports_an_unreachable_catalog(bank, served, monkeypatch):
    monkeypatch.setattr(bank.tools.catalog, "db", Down())
    status, _, answer = call(served, service.HEALTH_PATH, method="GET")
    assert status == 503 and answer == refusal("dependency_unavailable", service.CATALOG_DOWN)


def test_authenticate_matches_only_the_hashed_bearer_token():
    granted = Grant("p", "c", frozenset({"x"}))
    grants = {hashed("one"): granted}
    assert service.authenticate(grants, "Bearer one") is granted
    assert service.authenticate(grants, "Bearer two") is None
    assert service.authenticate(grants, f"Bearer {hashed('one')}") is None
    assert service.authenticate(grants, None) is None


def test_load_grants_reads_the_token_file(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({hashed("one"): {"principal": "p", "corpus": "c", "projects": ["b", "a"]}}))
    assert service.load_grants(path) == {hashed("one"): Grant("p", "c", frozenset({"a", "b"}))}


def environment(tmp_path, port="0", host="127.0.0.1"):
    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps({hashed(TOKEN): {"principal": "p", "corpus": "c", "projects": ["x"]}}))
    return {
        service.CATALOG_ENV: str(tmp_path / "catalog.sqlite3"),
        service.OBJECTS_ENV: str(tmp_path / "objects"),
        service.TOKENS_ENV: str(tokens),
        service.HOST_ENV: host,
        service.PORT_ENV: port,
    }


@pytest.mark.parametrize("missing", service.REQUIRED)
def test_build_refuses_a_missing_setting(tmp_path, missing):
    environ = {**environment(tmp_path), missing: ""}
    with pytest.raises(SystemExit) as raised:
        service.build(environ)
    assert raised.value.code == service.MISSING_CONFIG


def test_build_serves_the_configured_catalog_and_tokens(tmp_path):
    server = service.build(environment(tmp_path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        assert call(base, service.HEALTH_PATH, method="GET")[2] == {"state": "ready"}
        status, _, answer = call(
            base, service.TOOLS_PREFIX + "session_list", {"schema_version": SCHEMA_VERSION, "access_domain": "c"}, TOKEN
        )
        assert status == 200 and answer["state"] == "answered" and answer["total"] == 0
        assert call(base, service.SEARCH_PATH, {}, "other")[0] == 401
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert server.server_address[0] == "127.0.0.1"


def test_build_defaults_the_listener_address(tmp_path, monkeypatch):
    bound = []

    class Recorder:
        def __init__(self, address, handler):
            bound.append(address)

    monkeypatch.setattr(service, "HTTPServer", Recorder)
    environ = environment(tmp_path)
    del environ[service.HOST_ENV], environ[service.PORT_ENV]
    service.build(environ)
    assert bound == [(service.DEFAULT_HOST, service.DEFAULT_PORT)] == [("0.0.0.0", 8790)]


def test_build_binds_the_configured_port_over_tools_with_the_object_store(tmp_path, monkeypatch):
    bound, built = [], []

    class Recorder:
        def __init__(self, address, handler):
            bound.append(address)

    class Tools:
        def __init__(self, catalog, store):
            built.append(store)

    monkeypatch.setattr(service, "HTTPServer", Recorder)
    monkeypatch.setattr(service.records, "SessionTools", Tools)
    service.build(environment(tmp_path, port="8123"))
    assert bound == [("127.0.0.1", 8123)]
    assert [type(store) for store in built] == [service.archive.ObjectStore]


def test_main_serves_until_interrupted_and_closes(monkeypatch, capsys):
    events = []

    class Server:
        server_address, server_port = ("0.0.0.0", 8790), 8790

        def serve_forever(self):
            events.append("serve")
            raise KeyboardInterrupt

        def server_close(self):
            events.append("close")

    seen = []
    monkeypatch.setattr(service, "build", lambda environ: seen.append(environ) or Server())
    assert service.main() == 0
    assert seen == [dict(os.environ)]
    assert events == ["serve", "close"]
    assert capsys.readouterr().err == "session memory service listening on 0.0.0.0:8790\n"


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_the_container_command_serves_health(tmp_path):
    port = str(free_port())
    env = {**os.environ, **environment(tmp_path, port=port), "PYTHONPATH": str(ROOT)}
    process = subprocess.Popen(
        [sys.executable, "-m", "hooks.memory.service"], env=env, cwd=tmp_path, stderr=subprocess.PIPE
    )
    try:
        deadline, answer = time.monotonic() + 30, None
        while answer is None and time.monotonic() < deadline:
            try:
                answer = call(f"http://127.0.0.1:{port}", service.HEALTH_PATH, method="GET")
            except (urllib.error.URLError, ConnectionError):
                time.sleep(0.2)
        assert answer is not None and answer[0] == 200 and answer[2] == {"state": "ready"}
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_the_swarm_image_carries_the_service_and_its_postgres_driver():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "COPY hooks/ hooks/" in dockerfile
    assert any(
        line.startswith("psycopg2-binary==")
        for line in (ROOT / "docker/swarm/requirements.lock").read_text().splitlines()
    )
