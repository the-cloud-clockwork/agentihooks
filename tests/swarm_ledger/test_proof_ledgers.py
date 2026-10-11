import json
import logging
import os
from unittest.mock import patch

import pytest

from scripts.hive import auth
from scripts.swarm.naming import PROOF_RE
from scripts.swarm_ledger import ledger_authority as authority
from scripts.swarm_ledger import ledger_bin, proof_ledger
from scripts.swarm_ledger.api import proofs
from scripts.swarm_ledger.repository import repository
from tests.swarm_ledger.test_ledger_authority import SLUG, WORKER, send
from tests.swarm_ledger.test_ledger_authority import live as _authority_live

pytestmark = pytest.mark.xdist_group("fakeredis")

live = _authority_live
CONTENT = {"title": "Anton launch proof", "phases": [{"title": "Launch", "description": "One worker on Anton"}]}
JSON = {"Content-Type": "application/json"}


@pytest.fixture
def hive():
    import fakeredis

    redis = fakeredis.FakeRedis(decode_responses=True)
    with patch("scripts.swarm.store.redis_client", return_value=redis):
        yield redis


@pytest.fixture
def credential(hive):
    return auth.issue_controller(hive)


def post(live, path, payload, **headers):
    status, data, _ = send(live, "POST", path, json.dumps(payload).encode(), **JSON, **headers)
    return status, json.loads(data)


def create(live, credential, **fields):
    payload = {"agent": WORKER, "task": "t399", "content": CONTENT, **fields}
    return post(live, "/api/v1/proof-ledgers", payload, **{"X-Controller-Credential": credential})


def remove(live, slug, **headers):
    return post(live, f"/api/v1/proof-ledgers/{slug}/remove", {}, **headers)


def test_the_controller_credential_creates_a_proof_ledger_named_by_code(live, credential):
    status, reply = create(live, credential)
    slug = reply["data"]["slug"]
    assert (status, reply["data"]["created"]) == (200, True)
    assert PROOF_RE.fullmatch(slug) and slug.startswith("proof-323133-t399-")
    assert repository.get_document(slug)["title"] == "Anton launch proof"
    status, again = create(live, credential)
    assert status == 200 and again["data"]["slug"] not in (slug, "") and PROOF_RE.fullmatch(again["data"]["slug"])


def test_the_controller_credential_removes_a_proof_ledger_and_the_removal_is_recorded(live, credential, caplog):
    slug = create(live, credential)[1]["data"]["slug"]
    with caplog.at_level(logging.INFO, logger=proofs.__name__):
        status, reply = remove(live, slug, **{"X-Controller-Credential": credential})
    removed_at = ledger_bin.entries()[slug]
    assert (status, reply["data"]) == (200, {"slug": slug, "removed_at": removed_at})
    assert f"controller removed proof ledger {slug}" in caplog.text
    status, again = remove(live, slug, **{"X-Controller-Credential": credential})
    assert (status, again["data"]) == (200, {"slug": slug, "removed_at": removed_at})
    assert ledger_bin.entries()[slug] == removed_at


def other_credentials(live):
    return [
        {},
        {"X-Controller-Credential": "wrong"},
        {"X-Controller-Credential": ""},
        {"X-Ledger-Token": live["admin"]},
        {"X-Ledger-Token": authority.agent_token(live["admin"], SLUG, WORKER), "X-Ledger-Agent": WORKER},
    ]


def test_any_other_credential_is_refused_a_create(live, credential):
    before = {row["slug"] for row in repository.list_summaries()}
    for headers in other_credentials(live):
        payload = {"agent": WORKER, "task": "t399", "content": CONTENT}
        status, reply = post(live, "/api/v1/proof-ledgers", payload, **headers)
        assert (status, reply["error"]["code"]) == (403, "forbidden"), headers
        assert reply["error"]["message"] == "Proof ledgers need the controller credential"
    assert {row["slug"] for row in repository.list_summaries()} == before


def test_any_other_credential_is_refused_a_remove(live, credential):
    slug = create(live, credential)[1]["data"]["slug"]
    for headers in other_credentials(live):
        status, reply = remove(live, slug, **headers)
        assert (status, reply["error"]["code"]) == (403, "forbidden"), headers
    assert slug not in ledger_bin.entries()


def test_a_ledger_that_is_not_a_proof_ledger_is_refused_a_remove(live, credential):
    status, reply = remove(live, SLUG, **{"X-Controller-Credential": credential})
    assert (status, reply["error"]) == (
        403,
        {"code": "forbidden", "message": "Only a proof ledger is removed with the controller credential"},
    )
    assert SLUG not in ledger_bin.entries()
    assert repository.exists(SLUG)


def test_a_create_cannot_name_its_own_ledger(live, credential):
    status, reply = create(live, credential, slug=SLUG)
    assert (status, reply["error"]["code"]) == (400, "schema_invalid")


def test_a_create_needs_a_swarm_agent_and_task(live, credential):
    for fields in ({"agent": "someone"}, {"task": ""}):
        status, reply = create(live, credential, **fields)
        assert (status, reply["error"]["code"]) == (400, "schema_invalid"), fields
        assert "named from its swarm agent and task" in reply["error"]["message"]


def test_rejected_content_is_a_bad_request_and_the_server_keeps_serving(live, credential):
    status, reply = create(live, credential, content={"title": "No phases"})
    assert (status, reply["error"]) == (400, {"code": "schema_invalid", "message": "Content rejected: no phases"})
    assert create(live, credential)[0] == 200


def test_removing_a_missing_proof_ledger_is_not_found(live, credential):
    status, reply = remove(live, "proof-323133-t399-99", **{"X-Controller-Credential": credential})
    assert (status, reply["error"]["code"]) == (404, "ledger_missing")


def test_removing_stops_the_proof_ledgers_swarm(live, credential):
    slug = create(live, credential)[1]["data"]["slug"]
    from scripts.swarm_ledger import ledger_server

    with (
        patch.object(ledger_server, "swarm_status", return_value={"state": "running"}),
        patch.object(ledger_server, "swarm_control", return_value=(None, None)) as control,
    ):
        assert remove(live, slug, **{"X-Controller-Credential": credential})[0] == 200
    control.assert_called_once_with(slug, ["stop", "--now"])


def client_env(live, credential, **extra):
    return {
        "AGENTIHOOKS_DEPLOYMENT": "compose",
        "LEDGER_URL": f"http://{live['host']}",
        "AGENTIHOOKS_CONTROLLER_CREDENTIAL": credential,
        "AGENTIHOOKS_AGENT_NAME": WORKER,
        "AGENTIHOOKS_SWARM_TASK": "t399",
        **extra,
    }


def test_the_remote_client_creates_and_removes_a_proof_ledger_with_the_controller_credential(live, credential):
    environ = client_env(live, credential)
    created = proof_ledger.create(CONTENT, environ)
    assert created["created"] is True and PROOF_RE.fullmatch(created["slug"])
    removed = proof_ledger.remove(created["slug"], environ)
    assert removed == {"slug": created["slug"], "removed_at": ledger_bin.entries()[created["slug"]]}


def test_the_remote_client_needs_the_controller_credential(live, credential):
    environ = client_env(live, credential)
    environ.pop("AGENTIHOOKS_CONTROLLER_CREDENTIAL")
    with pytest.raises(SystemExit, match="AGENTIHOOKS_CONTROLLER_CREDENTIAL"):
        proof_ledger.create(CONTENT, environ)
    with pytest.raises(SystemExit, match="AGENTIHOOKS_CONTROLLER_CREDENTIAL"):
        proof_ledger.remove("proof-323133-t399-1", environ)


def test_the_remote_client_reports_a_refusal(live, credential):
    environ = client_env(live, "wrong")
    with pytest.raises(SystemExit, match="server refused: 403 Proof ledgers need the controller credential"):
        proof_ledger.create(CONTENT, environ)
    with pytest.raises(SystemExit, match="server refused: 403 Only a proof ledger"):
        proof_ledger.remove(SLUG, client_env(live, credential))


def test_the_proof_command_creates_and_removes_through_the_client(live, credential, tmp_path, capsys):
    path = tmp_path / "content.json"
    path.write_text(json.dumps(CONTENT))
    with patch.dict(os.environ, client_env(live, credential)):
        proof_ledger.main(["new", "--content", str(path)])
        slug = json.loads(capsys.readouterr().out)["slug"]
        proof_ledger.main(["remove", slug])
    assert json.loads(capsys.readouterr().out) == {"slug": slug, "removed_at": ledger_bin.entries()[slug]}
