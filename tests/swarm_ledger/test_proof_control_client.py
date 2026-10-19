import urllib.request

import pytest

from scripts.swarm_ledger import ledger, proof_ledger
from tests.swarm_ledger.test_ledger_authority import SLUG
from tests.swarm_ledger.test_proof_controls import credential as _credential
from tests.swarm_ledger.test_proof_controls import hive as _hive
from tests.swarm_ledger.test_proof_controls import live as _live
from tests.swarm_ledger.test_proof_controls import owned as _owned

credential, hive, live, owned = _credential, _hive, _live, _owned


@pytest.mark.parametrize("provided", [True, False])
def test_remote_creator_uses_its_own_parent_identity_and_bounded_transport(owned, monkeypatch, provided):
    environ = {
        "AGENTIHOOKS_DEPLOYMENT": "compose",
        "LEDGER_URL": f"http://{owned.live['host']}",
        "AGENTIHOOKS_AGENT_NAME": owned.owner,
        "AGENTIHOOKS_SWARM": SLUG,
    }
    if provided:
        environ["AGENTIHOOKS_LEDGER_AGENT_TOKEN"] = owned.parent["X-Ledger-Token"]
    minted = []

    def launch_token(slug, name):
        minted.append((slug, name))
        return owned.parent["X-Ledger-Token"]

    monkeypatch.setattr(ledger, "launch_token", launch_token)
    original = urllib.request.urlopen
    sent = []

    def open_request(request, **options):
        assert options == {"timeout": 10}
        sent.append(request.full_url)
        return original(request, **options)

    monkeypatch.setattr(urllib.request, "urlopen", open_request)
    monkeypatch.setattr(proof_ledger.ledger_link, "transport", lambda environ=None: {})
    result = proof_ledger.control(owned.slug, {"control": "pause"}, environ)
    assert result["state"] == "pending" and result["argv"] == ["pause"]
    assert minted == ([] if provided else [(SLUG, owned.owner)])
    assert sent == [
        f"http://{owned.live['host']}/api/v1/proof-ledgers/{owned.slug}/credential",
        f"http://{owned.live['host']}/api/v1/proof-ledgers/{owned.slug}/controls",
    ]
    assert "credential" not in result


@pytest.mark.parametrize("missing", ["AGENTIHOOKS_AGENT_NAME", "AGENTIHOOKS_SWARM", "both"])
def test_proof_client_names_its_missing_creator_identity(missing):
    environ = {"AGENTIHOOKS_AGENT_NAME": "engineer@a1b2c3-0001", "AGENTIHOOKS_SWARM": SLUG}
    if missing == "both":
        environ.clear()
    else:
        environ.pop(missing)
    with pytest.raises(SystemExit) as refused:
        proof_ledger.control("proof-a1b2c3-missing-1", {"control": "pause"}, environ)
    assert refused.value.code == "proof controls need the creating engineer identity and parent swarm"


def test_remote_client_reports_the_refusal_without_exposing_credentials(owned):
    environ = {
        "AGENTIHOOKS_DEPLOYMENT": "compose",
        "LEDGER_URL": f"http://{owned.live['host']}",
        "AGENTIHOOKS_AGENT_NAME": owned.owner,
        "AGENTIHOOKS_SWARM": SLUG,
        "AGENTIHOOKS_LEDGER_AGENT_TOKEN": owned.parent["X-Ledger-Token"],
    }
    with pytest.raises(SystemExit) as refused:
        proof_ledger.control(owned.slug, {"control": "cancel"}, environ)
    assert refused.value.code == "proof control refused: 403 Proof controls permit only pause, drain, set and start"
    assert owned.parent["X-Ledger-Token"] not in refused.value.code


def test_controller_client_adopts_external_owner_and_uses_scoped_controls(owned, monkeypatch):
    owner = {"parent": "external-parent", "agent": owned.owner, "task": "contract"}
    environ = {
        "AGENTIHOOKS_DEPLOYMENT": "compose",
        "LEDGER_URL": f"http://{owned.live['host']}",
        "AGENTIHOOKS_CONTROLLER_CREDENTIAL": owned.controller["X-Controller-Credential"],
    }
    owned.store.redis.delete(owned.store.key(owned.slug, "proof-creator"))
    original = urllib.request.urlopen
    sent = []

    def open_request(request, **options):
        assert options == {"timeout": 10}
        sent.append(request.full_url)
        return original(request, **options)

    monkeypatch.setattr(urllib.request, "urlopen", open_request)
    monkeypatch.setattr(proof_ledger.ledger_link, "transport", lambda environ=None: {})
    assert proof_ledger.migrate(owned.slug, owner, environ) == {"parent": "external-parent", "owner": owned.owner}
    result = proof_ledger.controller_control(owned.slug, owner, {"control": "pause"}, environ)
    assert result["state"] == "pending" and result["argv"] == ["pause"]
    assert "credential" not in result
    assert sent == [
        f"http://{owned.live['host']}/api/v1/proof-ledgers/{owned.slug}/migrate",
        f"http://{owned.live['host']}/api/v1/proof-ledgers/{owned.slug}/controller-controls",
    ]
