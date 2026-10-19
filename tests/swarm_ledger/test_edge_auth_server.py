import json
import uuid

import pytest

from scripts.swarm_ledger import ledger_authority as authority
from tests.swarm_ledger.test_edge_auth import AUD, FORGER, SEED, TEAM, assertion, fetched  # noqa: F401
from tests.swarm_ledger.test_ledger_authority import SLUG, WORKER, send
from tests.swarm_ledger.test_ledger_authority import live as _authority_live

live = _authority_live


@pytest.fixture
def edge(monkeypatch, fetched, live):  # noqa: F811
    for name, value in {
        "SWARM_AUTH_MODE": "edge",
        "SWARM_ACCESS_TEAM": TEAM,
        "SWARM_ACCESS_AUD": AUD,
        "SWARM_SECRET": SEED,
    }.items():
        monkeypatch.setenv(name, value)
    return live


def metadata(live, **headers):
    return send(live, "GET", f"/api/v1/ledgers/{SLUG}/metadata", **headers)[0]


def test_the_hosted_api_serves_the_operator_behind_access_and_refuses_the_page_credential(edge):
    assert metadata(edge, **{"Cf-Access-Jwt-Assertion": assertion()}) == 200
    assert metadata(edge, **{"Cf-Access-Jwt-Assertion": assertion(FORGER)}) == 403
    assert metadata(edge, **{"Cf-Access-Jwt-Assertion": assertion(aud=["another-app"])}) == 403
    assert metadata(edge, **{"X-Ledger-Token": edge["admin"]}) == 403


def test_the_hosted_api_serves_an_agent_with_its_server_secret_token(edge):
    token = authority.agent_token(SEED, SLUG, WORKER)
    assert metadata(edge, **{"X-Ledger-Token": token, "X-Ledger-Agent": WORKER}) == 200
    assert metadata(edge, **{"X-Ledger-Token": token + "0", "X-Ledger-Agent": WORKER}) == 403
    page_derived = authority.agent_token(edge["admin"], SLUG, WORKER)
    assert metadata(edge, **{"X-Ledger-Token": page_derived, "X-Ledger-Agent": WORKER}) == 403


def test_the_hosted_page_server_takes_an_operator_write_behind_access(edge):
    body = json.dumps({"ops": [{"op": "join", "id": uuid.uuid4().hex, "by": WORKER}]})
    headers = {"Content-Type": "application/json"}
    jwt_headers = {**headers, "Cf-Access-Jwt-Assertion": assertion()}
    assert send(edge, "PUT", f"/api/{SLUG}?view=agent", body, **jwt_headers)[0] == 200
    page_headers = {**headers, "X-Ledger-Token": edge["admin"]}
    assert send(edge, "PUT", f"/api/{SLUG}?view=agent", body, **page_headers)[0] == 403
