import io
import json
from email.message import Message
from types import SimpleNamespace

import pytest

from scripts.swarm import cli
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm_ledger import ledger_authority as authority
from scripts.swarm_ledger.api import routes
from tests.swarm_ledger.test_ledger_authority import SLUG, server
from tests.swarm_ledger.test_proof_ledgers import CONTENT
from tests.swarm_ledger.test_proof_ledgers import credential as _credential
from tests.swarm_ledger.test_proof_ledgers import hive as _hive
from tests.swarm_ledger.test_proof_ledgers import live as _live

pytestmark = pytest.mark.xdist_group("fakeredis")
credential, hive, live = _credential, _hive, _live


@pytest.fixture
def owned(live, credential, hive, monkeypatch):
    store = RedisStore(hive)
    store.create(SwarmConfig(SLUG, "/repo", 1, 0))
    owner = store.next_name(SLUG, "eng")
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "local")
    monkeypatch.setattr(server, "swarm_store", lambda: store)

    def invoke(resource, payload, headers):
        encoded = json.dumps(payload).encode()
        message = Message()
        for key, value in {
            "Host": live["host"],
            "Content-Type": "application/json",
            "Content-Length": str(len(encoded)),
            **headers,
        }.items():
            message[key] = value
        handler = SimpleNamespace(
            command="POST",
            path=f"/api/v1/proof-ledgers{resource}",
            headers=message,
            rfile=io.BytesIO(encoded),
            exists=server.repository.exists,
            send=lambda status, text, kind: (status, json.loads(text)),
        )
        return routes.handle(handler, server)

    controller = {"X-Controller-Credential": credential}
    status, created = invoke("", {"agent": owner, "task": "contract", "content": CONTENT}, controller)
    assert status == 200
    slug = created["data"]["slug"]
    store.create(SwarmConfig(slug, "/repo", 1, 0, scratch=True))
    parent = {"X-Ledger-Agent": owner, "X-Ledger-Token": authority.agent_token(live["admin"], SLUG, owner)}
    status, issued = invoke(f"/{slug}/credential", {}, parent)
    assert status == 200
    return SimpleNamespace(
        store=store,
        owner=owner,
        slug=slug,
        parent=parent,
        controller=controller,
        proof={"X-Proof-Credential": issued["data"]["credential"]},
        invoke=invoke,
        live=live,
    )


@pytest.mark.parametrize("resource", ["credential", "remove"])
@pytest.mark.parametrize("payload", [None, [], {"unexpected": True}])
def test_proof_identity_operations_require_an_empty_object(owned, resource, payload):
    headers = owned.parent if resource == "credential" else owned.controller
    assert owned.invoke(f"/{owned.slug}/{resource}", payload, headers) == (
        400,
        {"error": {"code": "schema_invalid", "message": "Request does not match the resource schema"}},
    )


@pytest.mark.parametrize("suffix", ["unknown", "remove/extra"])
def test_unknown_proof_routes_do_not_remove_the_fixture(owned, suffix):
    assert owned.invoke(f"/{owned.slug}/{suffix}", {}, owned.controller) == (
        404,
        {"error": {"code": "resource_missing", "message": "No such proof resource"}},
    )
    assert server.repository.exists(owned.slug)


@pytest.mark.parametrize("target", [SLUG, "proof-a1b2c3-missing-1"])
def test_absent_and_real_ledgers_refuse_proof_credentials(owned, target):
    assert owned.invoke(f"/{target}/credential", {}, owned.parent) == (
        403,
        {"error": {"code": "forbidden", "message": "Not an owned proof fixture"}},
    )


@pytest.mark.parametrize("lane", ["master", "ci"])
def test_nonengineer_creation_issues_no_owner_authority(owned, lane):
    name = owned.store.next_name(SLUG, lane)
    status, created = owned.invoke("", {"agent": name, "task": "nonengineer", "content": CONTENT}, owned.controller)
    assert status == 200
    headers = {"X-Ledger-Agent": name, "X-Ledger-Token": authority.agent_token(owned.live["admin"], SLUG, name)}
    assert owned.invoke(f"/{created['data']['slug']}/credential", {}, headers) == (
        403,
        {"error": {"code": "forbidden", "message": "Not an owned proof fixture"}},
    )


def test_missing_proof_refuses_even_its_creating_engineer(owned, monkeypatch):
    exists = server.repository.exists
    monkeypatch.setattr(server.repository, "exists", lambda slug: slug != owned.slug and exists(slug))
    assert owned.invoke(f"/{owned.slug}/credential", {}, owned.parent) == (
        403,
        {"error": {"code": "forbidden", "message": "Not an owned proof fixture"}},
    )


def test_removed_proof_loses_creator_authority_after_restore(owned, monkeypatch):
    monkeypatch.setattr(server, "swarm_status", lambda slug: None)
    status, removed = owned.invoke(f"/{owned.slug}/remove", {}, owned.controller)
    assert status == 200 and removed["data"]["slug"] == owned.slug
    server.repository.restore(owned.slug)
    assert server.repository.exists(owned.slug)
    assert owned.invoke(f"/{owned.slug}/credential", {}, owned.parent) == (
        403,
        {"error": {"code": "forbidden", "message": "Not an owned proof fixture"}},
    )


def test_missing_fixture_page_key_cannot_issue_control_authority(owned, monkeypatch):
    token = server.repository.token
    monkeypatch.setattr(server.repository, "token", lambda slug: None if slug == owned.slug else token(slug))
    assert owned.invoke(f"/{owned.slug}/credential", {}, owned.parent) == (
        403,
        {"error": {"code": "forbidden", "message": "Proof fixture has no credential"}},
    )


def test_only_the_creator_receives_a_named_credential_refusal(owned):
    assert owned.invoke(f"/{owned.slug}/credential", {}, {}) == (
        403,
        {"error": {"code": "forbidden", "message": "Only the creating engineer receives the fixture credential"}},
    )


@pytest.mark.parametrize("headers", [{}, {"X-Proof-Credential": "wrong"}])
def test_bad_fixture_credentials_have_a_named_refusal(owned, headers):
    assert owned.invoke(f"/{owned.slug}/controls", {"control": "pause"}, headers) == (
        403,
        {"error": {"code": "forbidden", "message": "Wrong proof fixture credential"}},
    )


def test_owned_fixture_cannot_control_a_real_swarm_configuration(owned):
    owned.store.update(owned.slug, scratch=False)
    assert owned.invoke(f"/{owned.slug}/controls", {"control": "pause"}, owned.proof) == (
        403,
        {"error": {"code": "forbidden", "message": "Proof controls need a scratch swarm"}},
    )


@pytest.mark.parametrize("action", ["cancel", "attach", "force_stop"])
def test_fixture_controls_refuse_every_ungranted_action(owned, action):
    assert owned.invoke(f"/{owned.slug}/controls", {"control": action}, owned.proof) == (
        403,
        {"error": {"code": "forbidden", "message": "Proof controls permit only pause and drain"}},
    )


def test_stale_drain_refusal_keeps_its_protocol_status_and_reason(owned):
    body = {
        "control": "drain",
        "agent": owned.owner,
        "op": "stale-drain",
        "incarnation": "stale",
        "generation": 1,
        "task_revision": 0,
    }
    assert owned.invoke(f"/{owned.slug}/controls", body, owned.proof) == (
        409,
        {
            "error": {
                "code": "control_refused",
                "message": "the page is stale: this agent was replaced, finished or moved task; wait for the next swarm view",
            }
        },
    )


def test_drain_service_refusal_keeps_its_protocol_status_and_reason(owned, monkeypatch):
    monkeypatch.setattr(server, "lifecycle_control", lambda slug, payload, operator: (None, "fixture unavailable"))
    assert owned.invoke(f"/{owned.slug}/controls", {"control": "drain"}, owned.proof) == (
        409,
        {"error": {"code": "control_refused", "message": "fixture unavailable"}},
    )


def test_existing_fixture_credential_remains_valid_for_its_original_scope(owned, monkeypatch):
    status, created = owned.invoke("", {"agent": owned.owner, "task": "vector", "content": CONTENT}, owned.controller)
    assert status == 200
    slug = created["data"]["slug"]
    owned.store.create(SwarmConfig(slug, "/repo", 1, 0, scratch=True))
    token = server.repository.token
    monkeypatch.setattr(server.repository, "token", lambda target: "fixture-page" if target == slug else token(target))
    assert (slug, owned.owner) == ("proof-a1b2c3-vector-1", "engineer@a1b2c3-0001")
    reference = "58f200d189280e75fc009eb29fdf02b9ccdd0ed9717c2dd70d1c907c9b86403d"
    status, issued = owned.invoke(f"/{slug}/credential", {}, owned.parent)
    assert status == 200 and issued["data"]["credential"] == reference
    status, accepted = owned.invoke(f"/{slug}/controls", {"control": "pause"}, {"X-Proof-Credential": reference})
    assert status == 200 and accepted["data"]["argv"] == ["pause"]


def test_proof_control_requires_its_request_file(capsys):
    with pytest.raises(SystemExit) as refused:
        cli.build_parser().parse_args(["proof-a1b2c3-contract-1", "proof-control"])
    assert refused.value.code == 2
    assert "--request" in capsys.readouterr().err
