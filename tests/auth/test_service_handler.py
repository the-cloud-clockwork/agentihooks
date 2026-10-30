import io
import json
from unittest.mock import patch

import jwt
import pytest

from scripts.auth import service, tokens
from scripts.swarm_ledger import ledger_authority as authority
from scripts.swarm_ledger.api.errors import APIError

SIGNING = "k" * 40
SLUG = "auth-proof"
WORKER = "engineer@323133-0256"


@pytest.fixture
def signing(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_AUTH_KEY", SIGNING)


class Recorded:
    def __init__(self) -> None:
        self.status = 0
        self.headers: list[tuple[str, str]] = []


def handler(path: str, headers: dict | None = None) -> tuple[service.Handler, Recorded]:
    bare = service.Handler.__new__(service.Handler)
    recorded = Recorded()
    bare.path = path
    bare.headers = headers or {}
    bare.wfile = io.BytesIO()
    bare.send_response = lambda status: setattr(recorded, "status", status)
    bare.send_header = lambda name, value: recorded.headers.append((name, value))
    bare.end_headers = lambda: None
    return bare, recorded


def body(bare: service.Handler) -> dict:
    return json.loads(bare.wfile.getvalue())


def test_a_reply_is_json_with_its_exact_length():
    bare, recorded = handler("/healthz")

    bare.send(201, {"ok": True})

    payload = bare.wfile.getvalue()
    assert recorded.status == 201
    assert recorded.headers == [("Content-Type", "application/json"), ("Content-Length", str(len(payload)))]
    assert json.loads(payload) == {"ok": True}


def test_the_health_route_answers_without_a_socket():
    bare, recorded = handler("/healthz")

    bare.do_GET()

    assert (recorded.status, body(bare)) == (200, {"ok": True})


def test_another_get_is_not_found_without_a_socket():
    bare, recorded = handler("/other")

    bare.do_GET()

    assert (recorded.status, body(bare)) == (404, {"error": {"code": "not_found", "message": "No such route"}})


def test_a_post_to_another_path_is_not_found_without_a_socket():
    bare, recorded = handler("/v1/ledgers/auth-proof/other")

    bare.do_POST()

    assert (recorded.status, body(bare)) == (404, {"error": {"code": "not_found", "message": "No such route"}})


def test_a_post_routes_the_token_exchange(signing):
    bare, recorded = handler(f"/v1/ledgers/{SLUG}/token?x=1", {"X-Ledger-Token": "t"})

    with patch.object(service, "exchange", return_value={"principal": ""}) as exchanged:
        bare.do_POST()

    exchanged.assert_called_once_with(bare.headers, SLUG)
    assert (recorded.status, body(bare)) == (200, {"data": {"principal": ""}})


def test_a_post_routes_the_agent_token(signing):
    bare, recorded = handler(f"/v1/ledgers/{SLUG}/agent-token")

    with patch.object(service, "agent_token", side_effect=APIError(403, "forbidden", "refused")) as issued:
        bare.do_POST()

    issued.assert_called_once_with(bare.headers, SLUG)
    assert (recorded.status, body(bare)) == (403, {"error": {"code": "forbidden", "message": "refused"}})


def test_exchange_reads_the_exact_controller_header(signing):
    headers = {"X-Controller-Credential": "held"}

    with (
        patch.object(authority, "credential_caller", return_value=None) as legacy,
        patch.object(authority, "controller_credential", return_value=True) as controller,
    ):
        reply = service.exchange(headers, SLUG)

    legacy.assert_called_once_with(headers, "", SLUG)
    controller.assert_called_once_with("held")
    assert reply["principal"] == ""
    assert tokens.verify(SIGNING, reply["token"], SLUG) == ""


def test_exchange_prefers_the_credential_principal(signing):
    with (
        patch.object(authority, "credential_caller", return_value=WORKER),
        patch.object(authority, "controller_credential", side_effect=AssertionError("checked the controller")),
    ):
        reply = service.exchange({}, SLUG)

    assert reply == {"principal": WORKER, "token": reply["token"], "expires_in": 900}
    assert jwt.decode(reply["token"], SIGNING, algorithms=["HS256"])["sub"] == WORKER


def test_agent_token_reads_the_exact_agent_and_hive_headers(signing):
    headers = {"X-Ledger-Agent": WORKER, "X-Hive-Credential": "member-credential"}

    with (
        patch.object(authority, "hive_member", return_value="m1") as member,
        patch.object(authority, "hive_agent", return_value=True) as placed,
        patch.object(authority, "hive_agent_token", return_value="hive.m1.x") as issued,
    ):
        reply = service.agent_token(headers, SLUG)

    member.assert_called_once_with("member-credential")
    placed.assert_called_once_with("m1", SLUG, WORKER)
    issued.assert_called_once_with("m1", SLUG, WORKER)
    assert (reply["agent"], reply["token"], reply["expires_in"]) == (WORKER, "hive.m1.x", 900)
    assert tokens.verify(SIGNING, reply["auth"], SLUG) == WORKER


def test_the_command_names_itself_in_its_usage(capsys):
    with pytest.raises(SystemExit):
        service.main(["--help"])

    assert capsys.readouterr().out.startswith("usage: python -m scripts.auth ")
