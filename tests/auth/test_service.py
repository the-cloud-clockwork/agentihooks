import json
import threading
import time
import urllib.error
import urllib.request
from unittest.mock import patch

import pytest

from scripts.auth import service, tokens
from scripts.hive import auth
from scripts.swarm_ledger import ledger_authority as authority

SIGNING = "k" * 40
SEED = "seed-one"
SLUG = "auth-proof"
WORKER = "engineer@323133-0256"


@pytest.fixture
def hive(monkeypatch):
    import fakeredis

    from scripts.swarm.store import RedisStore

    monkeypatch.setenv("AGENTIHOOKS_AUTH_KEY", SIGNING)
    monkeypatch.setenv("SWARM_AUTH_MODE", "edge")
    monkeypatch.setenv("SWARM_SECRET", SEED)
    monkeypatch.setenv("LEDGER_URL", "http://127.0.0.1:9")
    monkeypatch.delenv("AGENTIHOOKS_AUTH_LEGACY", raising=False)
    redis = fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    grant = auth.mint(redis, "member", "redis://localhost:6379/0")
    store = RedisStore(redis)
    store.names.adopt(SLUG, "323133", "", "")
    redis.hset(store.names.key("name", WORKER), mapping={"swarm": SLUG, "code": "323133"})
    with patch("scripts.swarm.store.redis_client", return_value=redis):
        yield store, grant


@pytest.fixture
def served(hive):
    httpd = service.server(("127.0.0.1", 0))
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def post(url: str, path: str, **headers: str) -> tuple[int, dict]:
    request = urllib.request.Request(f"{url}{path}", data=b"{}", headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as reply:
            return reply.status, json.loads(reply.read())
    except urllib.error.HTTPError as refused:
        return refused.code, json.loads(refused.read())


def placed(store, grant):
    from scripts.swarm.store import AgentRecord

    store.start_execution(SLUG, AgentRecord(WORKER, "eng", "proof", seat=f"eng-1@{SLUG}", hive=grant["id"]))


def hive_headers(grant, **extra: str) -> dict:
    return {"X-Hive-Credential": grant["ledger_credential"], **extra}


def test_a_hive_agent_authenticates_with_the_ledger_page_down(served, hive):
    store, grant = hive
    placed(store, grant)

    status, reply = post(served, f"/v1/ledgers/{SLUG}/agent-token", **hive_headers(grant, **{"X-Ledger-Agent": WORKER}))

    assert status == 200
    assert reply["data"]["agent"] == WORKER
    assert reply["data"]["token"].startswith(f"hive.{grant['id']}.")
    assert reply["data"]["expires_in"] == tokens.TTL_S
    signed = {"Authorization": f"Bearer {reply['data']['auth']}"}
    with patch("scripts.swarm.store.redis_client", side_effect=AssertionError("verified through Redis")):
        assert authority.caller(signed, "", SLUG) == WORKER
    assert auth.agent_member(store.redis, reply["data"]["token"], SLUG, WORKER) == grant["id"]


def test_an_agent_token_needs_the_hive_credential_and_a_placed_agent(served, hive):
    _, grant = hive
    route = f"/v1/ledgers/{SLUG}/agent-token"

    assert post(served, route, **hive_headers(grant)) == (
        403,
        {"error": {"code": "forbidden", "message": "An agent token names its agent in X-Ledger-Agent"}},
    )
    assert post(served, route, **{"X-Hive-Credential": "wrong", "X-Ledger-Agent": WORKER}) == (
        403,
        {"error": {"code": "forbidden", "message": "Missing or wrong hive credential"}},
    )
    assert post(served, route, **hive_headers(grant, **{"X-Ledger-Agent": WORKER})) == (
        403,
        {"error": {"code": "forbidden", "message": "Agent is not placed on this member's hive"}},
    )


def test_a_revoked_member_is_refused_an_agent_token(served, hive):
    store, grant = hive
    placed(store, grant)

    with patch.object(auth, "issue_agent", side_effect=auth.HiveError("Hive membership was revoked")):
        assert post(served, f"/v1/ledgers/{SLUG}/agent-token", **hive_headers(grant, **{"X-Ledger-Agent": WORKER})) == (
            403,
            {"error": {"code": "forbidden", "message": "Hive membership was revoked"}},
        )


def test_an_agent_credential_exchanges_for_a_signed_token(served):
    derived = {"X-Ledger-Token": authority.agent_token(SEED, SLUG, WORKER), "X-Ledger-Agent": WORKER}

    status, reply = post(served, f"/v1/ledgers/{SLUG}/token", **derived)

    assert status == 200
    assert reply["data"]["principal"] == WORKER
    assert reply["data"]["expires_in"] == tokens.TTL_S
    assert tokens.verify(SIGNING, reply["data"]["token"], SLUG) == WORKER


def test_tick_and_controller_credentials_exchange_for_operator_tokens(served, hive):
    store, _ = hive
    tick = {"X-Ledger-Tick": authority.tick_token(SEED, SLUG, int(time.time()) + 60)}
    controller = {"X-Controller-Credential": auth.issue_controller(store.redis)}

    for headers in (tick, controller):
        status, reply = post(served, f"/v1/ledgers/{SLUG}/token", **headers)
        assert status == 200
        assert tokens.verify(SIGNING, reply["data"]["token"], SLUG) == ""


def test_the_service_keeps_verifying_legacy_credentials_with_the_legacy_switch_off(served, hive, monkeypatch):
    store, _ = hive
    monkeypatch.setenv("AGENTIHOOKS_AUTH_LEGACY", "off")
    controller = {"X-Controller-Credential": auth.issue_controller(store.redis)}

    status, reply = post(served, f"/v1/ledgers/{SLUG}/token", **controller)

    assert status == 200
    assert tokens.verify(SIGNING, reply["data"]["token"], SLUG) == ""


def test_a_wrong_credential_is_refused(served):
    assert post(served, f"/v1/ledgers/{SLUG}/token", **{"X-Ledger-Token": "wrong", "X-Ledger-Agent": WORKER}) == (
        403,
        {"error": {"code": "forbidden", "message": "Missing or wrong credential"}},
    )


def test_the_service_refuses_to_sign_without_a_key(served, monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_AUTH_KEY")
    derived = {"X-Ledger-Token": authority.agent_token(SEED, SLUG, WORKER), "X-Ledger-Agent": WORKER}

    assert post(served, f"/v1/ledgers/{SLUG}/token", **derived) == (
        503,
        {"error": {"code": "unavailable", "message": "The auth service has no signing key"}},
    )


@pytest.mark.parametrize("path", ["/v1/ledgers/auth-proof/other", "/v1/ledgers/Bad_Slug/token", "/token"])
def test_an_unknown_route_is_not_found(served, path):
    assert post(served, path) == (404, {"error": {"code": "not_found", "message": "No such route"}})


def test_the_health_route_answers(served):
    with urllib.request.urlopen(f"{served}/healthz", timeout=10) as reply:
        assert (reply.status, json.loads(reply.read())) == (200, {"ok": True})
    with pytest.raises(urllib.error.HTTPError) as missing:
        urllib.request.urlopen(f"{served}/other", timeout=10)
    assert missing.value.code == 404


def test_main_serves_on_the_given_address():
    with patch.object(service, "server") as built:
        assert service.main(["serve", "--host", "0.0.0.0", "--port", "9000"]) == 0
    built.assert_called_once_with(("0.0.0.0", 9000))
    built.return_value.serve_forever.assert_called_once_with()


def test_main_defaults_to_loopback_on_the_auth_port():
    with patch.object(service, "server") as built:
        service.main(["serve"])
    built.assert_called_once_with(("127.0.0.1", 8768))
