import io
import json
import re
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

import scripts.swarm_v2.architecture as records
from scripts.swarm_ledger.api import architecture, routes
from scripts.swarm_ledger.api.errors import APIError
from tests.swarm_ledger.api_fakes import as_services

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "swarm_v2" / "architecture"
SLUG = "arch-2026-01-01"
PRINCIPALS = {"page-token": "", "agent-token": "engineer@1"}
SIGNER = Ed25519PrivateKey.from_private_bytes(b"k" * 32)
PUBLIC_HEX = SIGNER.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
DISPATCHER = {"id": "duplicate-dispatcher", "name": "Kubernetes task dispatcher", "kind": "dispatcher"}


class Headers(dict):
    def get_content_type(self):
        return self.get("Content-Type", "text/plain")


def fake_server():
    return as_services(server_parts())


def server_parts():
    return SimpleNamespace(
        ALLOWED_HOSTS={"h"},
        ALLOWED_ORIGINS=set(),
        MAX_BODY=10_000,
        core=SimpleNamespace(SLUG_RE=re.compile(r"[a-z0-9-]+"), loads=json.loads),
        authority=SimpleNamespace(
            caller=lambda headers, admin, slug: (
                headers.get("X-Ledger-Agent") or PRINCIPALS.get(headers.get("X-Ledger-Token"))
            ),
            controller=lambda credential: False,
        ),
        repository=SimpleNamespace(token={SLUG: "page-token"}.get),
    )


def request(method, path, payload=None, token="page-token", agent=None):
    raw = b"" if payload is None else json.dumps(payload).encode()
    headers = Headers({"Host": "h", "Content-Type": "application/json", "Content-Length": str(len(raw))})
    headers["X-Ledger-Token"] = token
    if agent is not None:
        headers["X-Ledger-Agent"] = agent
    return SimpleNamespace(
        headers=headers,
        path=f"/api/v1/ledgers/{SLUG}/{path}",
        command=method,
        rfile=io.BytesIO(raw),
        exists=lambda name: name == SLUG,
    )


@pytest.fixture
def files(tmp_path, monkeypatch):
    record = tmp_path / "architecture.json"
    shutil.copy(ROOT / records.RECORD, record)
    inventory = tmp_path / "inventory.json"
    shutil.copy(FIXTURES / "inventory.json", inventory)
    monkeypatch.setenv("AGENTIHOOKS_ARCHITECTURE_RECORD", str(record))
    monkeypatch.setenv("AGENTIHOOKS_ARCHITECTURE_INVENTORY", str(inventory))
    monkeypatch.setenv("SWARM_ARCHITECTURE_SIGNING_KEY", (b"k" * 32).hex())
    monkeypatch.setenv("SWARM_ARCHITECTURE_PUBLIC_KEY", PUBLIC_HEX)
    return SimpleNamespace(record=record, inventory=inventory, markdown=tmp_path / "decisions.md")


def act(payload, token="page-token", agent=None):
    return routes.dispatch(request("POST", "architecture/actions", payload, token, agent), fake_server())


def refused(payload, token="page-token", agent=None):
    with pytest.raises(APIError) as error:
        act(payload, token, agent)
    return error.value.status, error.value.code, str(error.value)


def second_revision(files):
    records.apply_inventory(files.record, records.load_inventory(files.inventory))


def test_the_page_reads_the_revision_the_proposals_awaiting_approval_and_earlier_revisions(files):
    second_revision(files)
    reply = routes.dispatch(request("GET", "architecture"), fake_server())
    assert reply["data"] == {
        "revision": 2,
        "components": 13,
        "proposals": [{**DISPATCHER, "approved": False}],
        "revisions": [{"revision": 1, "operation": ""}],
    }
    assert reply["revision"] == architecture.resources.revision(reply["data"])


def test_an_agent_may_read_the_record(files):
    reply = routes.dispatch(request("GET", "architecture", token="agent-token", agent="engineer@1"), fake_server())
    assert reply["data"]["revision"] == 1


def test_the_operator_approves_through_the_page_credential(files):
    reply = act({"action": "approve", "proposal": "duplicate-dispatcher", "reason": "operator moves dispatch"})
    assert reply["data"]["proposals"] == [{**DISPATCHER, "approved": True}]
    [change] = records.load_record(files.record)["operator_changes"]
    assert (change["proposal"], change["approved_by"], change["reason"]) == (
        "duplicate-dispatcher",
        "operator",
        "operator moves dispatch",
    )
    assert files.markdown.read_text() == records.render(records.load_record(files.record), SIGNER.public_key())


def test_the_operator_rolls_back_to_an_earlier_revision(files):
    second_revision(files)
    reply = act({"action": "rollback", "to": 1})
    record = records.load_record(files.record)
    assert record["revision"] == 3
    assert record["operations"][-1]["id"] == "page-rollback-2-to-1"
    assert "Brain arc embedding backlog" not in [c["name"] for c in record["components"]]
    assert reply["data"]["revisions"] == [
        {"revision": 2, "operation": "fixture-inventory-1"},
        {"revision": 1, "operation": ""},
    ]
    assert files.markdown.read_text() == records.render(record, SIGNER.public_key())


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "approve", "proposal": "duplicate-dispatcher", "reason": "agent asks"},
        {"action": "rollback", "to": 1},
    ],
)
def test_an_agent_is_refused_both_actions_and_the_record_is_unchanged(files, payload):
    second_revision(files)
    before = files.record.read_bytes()
    assert refused(payload, token="agent-token", agent="engineer@1") == (
        403,
        "forbidden",
        "Architecture changes need the operator",
    )
    assert files.record.read_bytes() == before
    assert not files.markdown.exists()


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "approve", "proposal": "duplicate-dispatcher", "reason": "agent asks"},
        {"action": "rollback", "to": 1},
    ],
)
def test_an_agent_holding_the_page_credential_is_refused_through_the_route(files, payload):
    second_revision(files)
    before = files.record.read_bytes()
    assert refused(payload, token="page-token", agent="engineer@1") == (
        403,
        "forbidden",
        "Architecture changes need the operator",
    )
    assert files.record.read_bytes() == before


def test_the_page_reads_an_approval_signed_by_the_configured_key(files):
    act({"action": "approve", "proposal": "duplicate-dispatcher", "reason": "operator moves dispatch"})
    reply = routes.dispatch(request("GET", "architecture"), fake_server())
    assert reply["data"]["proposals"] == [{**DISPATCHER, "approved": True}]


def test_an_approval_without_a_configured_public_key_reads_with_the_signing_key(files, monkeypatch):
    monkeypatch.delenv("SWARM_ARCHITECTURE_PUBLIC_KEY")
    reply = act({"action": "approve", "proposal": "duplicate-dispatcher", "reason": "operator moves dispatch"})
    assert reply["data"]["proposals"] == [{**DISPATCHER, "approved": True}]
    assert "Operator architecture changes: duplicate-dispatcher by operator" in files.markdown.read_text()


def test_a_rollback_keeps_signed_approvals_in_the_decisions(files):
    second_revision(files)
    act({"action": "approve", "proposal": "duplicate-dispatcher", "reason": "operator moves dispatch"})
    act({"action": "rollback", "to": 1})
    assert "Operator architecture changes: duplicate-dispatcher by operator at revision 2" in (
        files.markdown.read_text()
    )


def test_the_operator_principal_still_needs_the_page_credential_of_that_ledger(files):
    handler = request("POST", "architecture/actions")
    server = as_services(SimpleNamespace(repository=SimpleNamespace(token={"other": "page-token"}.get)))
    with pytest.raises(APIError) as error:
        architecture.operator_transport(handler, server, SLUG, "")
    assert (error.value.status, str(error.value)) == (403, "Architecture changes need the operator")


def test_an_agent_principal_is_refused_even_with_the_page_credential(files):
    with pytest.raises(APIError) as error:
        architecture.operator_transport(request("POST", "architecture/actions"), fake_server(), SLUG, "engineer@1")
    assert (error.value.status, error.value.code, str(error.value)) == (
        403,
        "forbidden",
        "Architecture changes need the operator",
    )


def test_the_operator_transport_carries_the_page_credential(files):
    transport = architecture.operator_transport(request("POST", "architecture/actions"), fake_server(), SLUG, "")
    assert (transport["slug"], transport["credential"]) == (SLUG, "page-token")
    assert transport["authenticate"](SLUG, "page-token").role.value == "operator"


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "approve", "proposal": "duplicate-dispatcher"},
        {"action": "approve", "proposal": "duplicate-dispatcher", "reason": ""},
        {"action": "rollback", "to": 0},
        {"action": "rollback", "to": 1, "extra": True},
        {"action": "delete"},
    ],
)
def test_a_malformed_action_is_refused(files, payload):
    assert refused(payload)[:2] == (400, "schema_invalid")


def test_a_refused_architecture_change_names_its_reason(files):
    before = files.record.read_bytes()
    assert refused({"action": "rollback", "to": 1}) == (
        409,
        "architecture_refused",
        "rollback target 1 is not an earlier revision of 1",
    )
    assert files.record.read_bytes() == before


def test_approval_needs_a_configured_inventory(files, monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_ARCHITECTURE_INVENTORY")
    payload = {"action": "approve", "proposal": "duplicate-dispatcher", "reason": "r"}
    assert refused(payload) == (409, "architecture_refused", "no design inventory is configured")
    assert routes.dispatch(request("GET", "architecture"), fake_server())["data"]["proposals"] == []


def test_the_record_defaults_to_the_committed_one(monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_ARCHITECTURE_RECORD", raising=False)
    monkeypatch.delenv("AGENTIHOOKS_ARCHITECTURE_INVENTORY", raising=False)
    assert architecture.paths({}) == (ROOT / "docs" / "swarm-v2" / "architecture.json", None)
