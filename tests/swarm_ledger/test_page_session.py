"""The ledger page carries no operator token: the browser holds an operator session cookie instead."""

import http.client
import json
import re
import subprocess
import threading
import uuid
from collections.abc import Iterator
from contextlib import AbstractContextManager
from http.cookies import SimpleCookie
from unittest.mock import patch

import pytest
from _pytest.monkeypatch import MonkeyPatch

from scripts.auth import service as auth_service
from scripts.auth import tokens
from scripts.swarm_ledger import ledger_authority as authority
from tests.swarm_ledger.test_ledger_authority import SLUG, send, server
from tests.swarm_ledger.test_ledger_authority import live as _authority_live

live = _authority_live
KEY = "k" * 40


def origin(live: dict) -> dict:
    """An allowed browser origin for the live server."""
    return {"Origin": f"http://{live['host']}"}


def allowed(live: dict) -> AbstractContextManager:
    """Allow the live server's own origin."""
    return patch.object(server, "ALLOWED_ORIGINS", {f"http://{live['host']}"})


def open_session(live: dict, **headers: str) -> tuple[int, SimpleCookie, bytes]:
    """POST the page session for SLUG and return the status, the cookies it set and the body."""
    status, body, _, cookie = send_with_cookie(live, "POST", f"/api/{SLUG}/session", **{**origin(live), **headers})
    return status, cookie, body


def send_with_cookie(
    live: dict, method: str, path: str, body: bytes = b"", **headers: str
) -> tuple[int, bytes, str, SimpleCookie]:
    """send, also returning the response's Set-Cookie header parsed."""
    conn = http.client.HTTPConnection("127.0.0.1", live["port"], timeout=5)
    try:
        conn.request(method, path, body, {"Host": live["host"], **headers})
        response = conn.getresponse()
        return (
            response.status,
            response.read(),
            response.getheader("Set-Cookie") or "",
            SimpleCookie(response.getheader("Set-Cookie") or ""),
        )
    finally:
        conn.close()


def write(live: dict, **headers: str) -> int:
    """An operator chat write carrying only the given headers as its credential."""
    op = {"op": "add", "id": uuid.uuid4().hex, "thread": "chat", "text": "Through the session"}
    body = json.dumps({"ops": [op]})
    status, _, _ = send(live, "PUT", f"/api/{SLUG}?view=agent", body, **{"Content-Type": "application/json", **headers})
    return status


def test_no_served_ledger_page_carries_the_ledger_token(live: dict) -> None:
    status, body, _ = send(live, "GET", f"/{SLUG}")

    assert status == 200
    assert live["admin"] not in body.decode()
    assert "ledger-token" not in body.decode()
    assert live["admin"] not in server.page_for(SLUG)


def test_a_session_cookie_alone_carries_operator_writes(live: dict) -> None:
    with allowed(live):
        status, cookie, _ = open_session(live)
    name = f"ledger-session-{SLUG}"

    assert status == 200
    assert name in cookie
    assert cookie[name]["httponly"] and cookie[name]["samesite"] == "Strict" and cookie[name]["path"] == "/"
    assert write(live, Cookie=f"{name}={cookie[name].value}") == 200
    assert write(live) == 403
    assert write(live, Cookie=f"ledger-session-other={cookie[name].value}") == 403


def test_a_session_needs_an_allowed_origin(live: dict) -> None:
    with allowed(live):
        status, cookie, _ = open_session(live, Origin="http://elsewhere.test")

    assert status == 403
    assert not cookie


@pytest.fixture
def auth(live: dict, monkeypatch: MonkeyPatch) -> Iterator[dict]:
    """An auth service with a signing key, legacy credentials off, the ledger in edge mode."""
    httpd = auth_service.server(("127.0.0.1", 0))
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    monkeypatch.setenv(tokens.KEY_ENV, KEY)
    monkeypatch.setenv("AGENTIHOOKS_AUTH_URL", f"http://127.0.0.1:{httpd.server_address[1]}")
    monkeypatch.setenv("AGENTIHOOKS_AUTH_LEGACY", "off")
    monkeypatch.setenv("SWARM_AUTH_MODE", "edge")
    try:
        with patch.object(authority, "access_operator", lambda assertion: assertion == "operator-assertion"):
            yield live
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_the_auth_service_issues_the_signed_operator_session(auth: dict) -> None:
    with allowed(auth):
        status, cookie, _ = open_session(auth, **{"Cf-Access-Jwt-Assertion": "operator-assertion"})
    name = f"ledger-session-{SLUG}"

    kind, _, token = cookie[name].value.partition(":")

    assert status == 200
    assert kind == "bearer"
    assert tokens.verify(KEY, token, SLUG) == ""
    assert cookie[name]["secure"]
    assert int(cookie[name]["max-age"]) > 0
    assert write(auth, Cookie=f"{name}={cookie[name].value}") == 200
    assert write(auth, **{"Cf-Access-Jwt-Assertion": "operator-assertion"}) == 403


def test_the_auth_service_refuses_a_session_without_the_operator(auth: dict) -> None:
    with allowed(auth):
        status, cookie, _ = open_session(auth, **{"Cf-Access-Jwt-Assertion": "someone"})

    assert status == 403
    assert not cookie


def test_edge_mode_without_an_auth_service_opens_no_session(live: dict, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTIHOOKS_AUTH_URL", raising=False)
    monkeypatch.setenv("SWARM_AUTH_MODE", "edge")
    with allowed(live):
        status, cookie, _ = open_session(live)

    assert status == 503
    assert not cookie


def test_local_mode_with_an_auth_service_keeps_the_page_session(live: dict, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTIHOOKS_AUTH_URL", "http://127.0.0.1:9")
    monkeypatch.delenv("SWARM_AUTH_MODE", raising=False)
    monkeypatch.delenv("AGENTIHOOKS_AUTH_LEGACY", raising=False)
    with allowed(live):
        status, cookie, _ = open_session(live)
    name = f"ledger-session-{SLUG}"

    assert status == 200
    assert cookie[name].value.startswith("page:")
    assert not cookie[name]["secure"]
    assert write(live, Cookie=f"{name}={cookie[name].value}") == 200


def test_an_unmarked_cookie_carries_no_credential(live: dict) -> None:
    with allowed(live):
        _, cookie, _ = open_session(live)
    name = f"ledger-session-{SLUG}"
    _, _, credential = cookie[name].value.partition(":")

    assert cookie[name].value.startswith("page:")
    assert write(live, Cookie=f"{name}={credential}") == 403
    assert write(live, Cookie=f"{name}=bearer:{credential}") == 403


def test_the_page_renews_its_session_before_it_expires() -> None:
    api = (server.core.HOME.parent / "static" / "js" / "api.js").read_text(encoding="utf-8")
    functions = "\n".join(
        re.search(rf"(export (?:async )?function {name}\(.*?\n\}})", api, re.S).group(1).removeprefix("export ")
        for name in ("openSession", "keepSession")
    )
    probe = (
        "const SESSION_ROOT='/api'; const SLUG='sw'; const timers=[]; const calls=[];\n"
        "global.setTimeout=(fn,ms)=>timers.push([fn.name,ms]);\n"
        "let replies=[{ok:true,json:async()=>({expires_in:900})},{ok:false},null];\n"
        "global.fetch=async (url)=>{calls.push(url); const r=replies.shift(); if(!r) throw new Error('down'); return r;};\n"
        + functions
        + "\n(async()=>{await keepSession(); await keepSession(); await keepSession();"
        "process.stdout.write(JSON.stringify({timers,calls}));})();"
    )
    result = subprocess.run(["node", "-e", probe], capture_output=True, text=True, check=True)
    observed = json.loads(result.stdout)

    assert observed["calls"] == ["/api/sw/session"] * 3
    assert observed["timers"] == [["keepSession", 720000], ["keepSession", 30000], ["keepSession", 30000]]
