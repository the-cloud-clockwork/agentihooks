from unittest.mock import patch

import pytest

from scripts.auth import tokens
from scripts.swarm_ledger import ledger_authority as authority

SIGNING = "k" * 40
SEED = "seed-one"
SLUG = "authority-proof"
WORKER = "engineer@323133-0256"


@pytest.fixture
def edge(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_AUTH_KEY", SIGNING)
    monkeypatch.setenv("SWARM_AUTH_MODE", "edge")
    monkeypatch.setenv("SWARM_SECRET", SEED)
    monkeypatch.delenv("AGENTIHOOKS_AUTH_LEGACY", raising=False)


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("mode", ["edge", "local"])
def test_a_signed_token_names_its_principal_in_either_mode(edge, monkeypatch, mode):
    monkeypatch.setenv("SWARM_AUTH_MODE", mode)

    assert authority.caller(bearer(tokens.sign(SIGNING, WORKER, SLUG)), "page", SLUG) == WORKER
    assert authority.caller(bearer(tokens.sign(SIGNING, "", SLUG)), "page", SLUG) == ""


def test_a_signed_token_for_another_ledger_or_key_is_refused(edge):
    assert authority.caller(bearer(tokens.sign(SIGNING, WORKER, "another-ledger")), "page", SLUG) is None
    assert authority.caller(bearer(tokens.sign("x" * 40, WORKER, SLUG)), "page", SLUG) is None


def test_a_bearer_token_never_falls_back_to_the_legacy_headers(edge):
    headers = {
        **bearer("forged"),
        "X-Ledger-Token": authority.agent_token(SEED, SLUG, WORKER),
        "X-Ledger-Agent": WORKER,
    }

    assert authority.caller(headers, "page", SLUG) is None


def test_a_signed_token_is_refused_without_a_key(edge, monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_AUTH_KEY")

    assert authority.caller(bearer(tokens.sign(SIGNING, WORKER, SLUG)), "page", SLUG) is None


def test_legacy_credentials_pass_until_the_switch_is_off(edge, monkeypatch):
    derived = {"X-Ledger-Token": authority.agent_token(SEED, SLUG, WORKER), "X-Ledger-Agent": WORKER}

    assert authority.caller(derived, "page", SLUG) == WORKER
    monkeypatch.setenv("AGENTIHOOKS_AUTH_LEGACY", "off")
    assert authority.caller(derived, "page", SLUG) is None
    assert authority.credential_caller(derived, "page", SLUG) == WORKER
    assert authority.caller(bearer(tokens.sign(SIGNING, WORKER, SLUG)), "page", SLUG) == WORKER


def test_the_controller_credential_passes_until_the_switch_is_off(monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_AUTH_LEGACY", raising=False)
    with patch.object(authority, "controller_credential", return_value=True) as checked:
        assert authority.controller("held") is True
        monkeypatch.setenv("AGENTIHOOKS_AUTH_LEGACY", "off")
        assert authority.controller("held") is False
    checked.assert_called_once_with("held")


@pytest.mark.parametrize("credential", ["", None])
def test_an_empty_controller_credential_is_refused_without_a_lookup(credential):
    with patch.object(authority.ports, "hive", side_effect=AssertionError("looked up")):
        assert authority.controller_credential(credential) is False
