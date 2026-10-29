import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from scripts.swarm_ledger import ledger_authority as authority

SLUG = "edge-proof"
WORKER = "engineer@323133-0256"
OTHER = "engineer@323133-0257"
TEAM = "anton"
ISSUER = "https://anton.cloudflareaccess.com"
AUD = "swarm-audience"
SEED = "fixture-seed"
PAGE = "fixture-page"


def keypair():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    return private, {**public, "kid": "k1", "alg": "RS256", "use": "sig"}


SIGNER, JWK = keypair()
FORGER, _ = keypair()


def assertion(key=SIGNER, **claims):
    now = int(time.time())
    body = {"aud": [AUD], "iss": ISSUER, "iat": now, "exp": now + 300, "email": "operator@example.com", **claims}
    return jwt.encode(body, key, algorithm="RS256", headers={"kid": "k1"})


@pytest.fixture
def fetched(monkeypatch):
    urls = []

    def fetch_data(client):
        urls.append(client.uri)
        return {"keys": [JWK]}

    authority.access_keys.cache_clear()
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", fetch_data)
    yield urls
    authority.access_keys.cache_clear()


@pytest.fixture
def edge(monkeypatch, fetched):
    for name, value in {
        "SWARM_AUTH_MODE": "edge",
        "SWARM_ACCESS_TEAM": TEAM,
        "SWARM_ACCESS_AUD": AUD,
        "SWARM_SECRET": SEED,
    }.items():
        monkeypatch.setenv(name, value)
    return fetched


def headers(token=None, agent=None, jwt_assertion=None):
    given = {"X-Ledger-Token": token, "X-Ledger-Agent": agent, "Cf-Access-Jwt-Assertion": jwt_assertion}
    return {key: value for key, value in given.items() if value is not None}


def test_local_mode_keeps_the_page_credential_and_its_agent_tokens(monkeypatch):
    monkeypatch.delenv("SWARM_AUTH_MODE", raising=False)
    monkeypatch.setenv("SWARM_SECRET", SEED)
    assert authority.caller(headers(PAGE), PAGE, SLUG) == ""
    assert authority.caller(headers(authority.agent_token(PAGE, SLUG, WORKER), WORKER), PAGE, SLUG) == WORKER
    assert authority.caller(headers(authority.agent_token(SEED, SLUG, WORKER), WORKER), PAGE, SLUG) is None
    assert authority.caller(headers(jwt_assertion=assertion()), PAGE, SLUG) is None


def test_an_unknown_mode_is_local(monkeypatch):
    monkeypatch.setenv("SWARM_AUTH_MODE", "cloud")
    assert authority.caller(headers(PAGE), PAGE, SLUG) == ""


def test_edge_mode_takes_a_verified_access_assertion_as_the_operator(edge):
    assert authority.caller(headers(jwt_assertion=assertion()), PAGE, SLUG) == ""
    assert edge == [f"{ISSUER}/cdn-cgi/access/certs"]


def test_edge_mode_refuses_a_forged_assertion(edge):
    assert authority.caller(headers(jwt_assertion=assertion(FORGER)), PAGE, SLUG) is None


def test_edge_mode_refuses_a_wrong_audience(edge):
    assert authority.caller(headers(jwt_assertion=assertion(aud=["another-app"])), PAGE, SLUG) is None


def test_edge_mode_refuses_a_wrong_issuer_and_an_expired_assertion(edge):
    wrong_issuer = assertion(iss="https://other.cloudflareaccess.com")
    assert authority.caller(headers(jwt_assertion=wrong_issuer), PAGE, SLUG) is None
    assert authority.caller(headers(jwt_assertion=assertion(exp=int(time.time()) - 60)), PAGE, SLUG) is None
    assert authority.caller(headers(jwt_assertion="not-a-jwt"), PAGE, SLUG) is None


def test_edge_mode_refuses_an_assertion_signed_with_another_algorithm(edge):
    forged = jwt.encode({"aud": [AUD], "iss": ISSUER}, "x" * 32, algorithm="HS256", headers={"kid": "k1"})
    assert authority.caller(headers(jwt_assertion=forged), PAGE, SLUG) is None


def test_edge_mode_refuses_the_page_credential(edge):
    assert authority.caller(headers(PAGE), PAGE, SLUG) is None
    assert authority.caller(headers(authority.agent_token(PAGE, SLUG, WORKER), WORKER), PAGE, SLUG) is None


def test_edge_mode_takes_an_agent_token_derived_from_the_server_secret(edge):
    token = authority.agent_token(SEED, SLUG, WORKER)
    assert authority.caller(headers(token, WORKER), PAGE, SLUG) == WORKER
    assert authority.caller(headers(token, OTHER), PAGE, SLUG) is None
    assert authority.caller(headers(token, WORKER), PAGE, "other-ledger") is None
    assert authority.caller(headers(token + "0", WORKER), PAGE, SLUG) is None
    assert authority.caller(headers(token), PAGE, SLUG) is None
    assert authority.caller(headers(agent=WORKER), PAGE, SLUG) is None


def test_edge_mode_refuses_agent_tokens_without_a_server_secret(edge, monkeypatch):
    monkeypatch.delenv("SWARM_SECRET")
    assert authority.caller(headers(authority.agent_token("", SLUG, WORKER), WORKER), PAGE, SLUG) is None


def test_edge_mode_checks_hive_tokens_through_the_hive_authority(edge, monkeypatch):
    monkeypatch.setattr(authority, "hive_principal", lambda *given: given)
    assert authority.caller(headers("hive.abc", WORKER), PAGE, SLUG) == (SLUG, "hive.abc", WORKER)


def test_edge_mode_refuses_an_empty_agent_identity(edge):
    token = authority.agent_token(SEED, SLUG, "")
    assert authority.caller(headers(token, ""), PAGE, SLUG) is None


def test_edge_mode_checks_hive_tokens_without_a_server_secret(edge, monkeypatch):
    monkeypatch.delenv("SWARM_SECRET")
    seen = []

    def verify(slug, token, agent):
        seen.append((slug, token, agent))
        return WORKER if token == "hive.valid" else None

    monkeypatch.setattr(authority, "hive_principal", verify)
    assert authority.caller(headers("hive.valid", WORKER), PAGE, SLUG) == WORKER
    assert authority.caller(headers("hive.invalid", WORKER), PAGE, SLUG) is None
    assert seen == [(SLUG, "hive.valid", WORKER), (SLUG, "hive.invalid", WORKER)]


def test_edge_mode_never_takes_the_server_secret_itself_as_the_operator(edge, monkeypatch):
    monkeypatch.setenv("SWARM_SECRET", "hive.seed")
    monkeypatch.setattr(authority, "hive_principal", lambda *given: None)
    assert authority.caller(headers("hive.seed", WORKER), PAGE, SLUG) is None


def test_edge_mode_refuses_an_assertion_without_an_expiry(edge):
    now = int(time.time())
    unbounded = jwt.encode({"aud": [AUD], "iss": ISSUER, "iat": now}, SIGNER, algorithm="RS256", headers={"kid": "k1"})
    assert authority.caller(headers(jwt_assertion=unbounded), PAGE, SLUG) is None


@pytest.mark.parametrize("failure", [OSError("tls"), ValueError("not json")])
def test_edge_mode_refuses_an_assertion_when_the_certs_cannot_be_read(edge, monkeypatch, failure):
    def broken(client):
        raise failure

    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", broken)
    assert authority.caller(headers(jwt_assertion=assertion()), PAGE, SLUG) is None


@pytest.mark.parametrize("missing", ["SWARM_ACCESS_TEAM", "SWARM_ACCESS_AUD"])
def test_edge_mode_refuses_every_assertion_without_its_access_settings(edge, monkeypatch, missing):
    monkeypatch.delenv(missing)
    assert authority.caller(headers(jwt_assertion=assertion()), PAGE, SLUG) is None
    assert edge == []


@pytest.mark.parametrize("team", ["anton.cloudflareaccess.com", "https://anton.cloudflareaccess.com"])
def test_a_full_team_domain_is_the_issuer(edge, monkeypatch, team):
    monkeypatch.setenv("SWARM_ACCESS_TEAM", team)
    assert authority.caller(headers(jwt_assertion=assertion()), PAGE, SLUG) == ""
    assert edge == [f"{ISSUER}/cdn-cgi/access/certs"]


def test_access_keys_are_one_cached_client_per_certs_url(fetched):
    client = authority.access_keys(f"{ISSUER}/cdn-cgi/access/certs")
    assert client is authority.access_keys(f"{ISSUER}/cdn-cgi/access/certs")
    assert (client.uri, client.timeout) == (f"{ISSUER}/cdn-cgi/access/certs", 10)
    assert authority.access_keys.cache_info().maxsize == 4
