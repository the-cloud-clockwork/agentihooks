import json
import os
import urllib.request
from unittest.mock import patch

import pytest

from scripts.hive import auth
from scripts.swarm_ledger import ledger_authority as authority
from scripts.swarm_ledger import ledger_bin, proof_ledger, run
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


def create(live, credential, task, **fields):
    payload = {"agent": WORKER, "task": task, "content": CONTENT, **fields}
    return post(live, "/api/v1/proof-ledgers", payload, **{"X-Controller-Credential": credential})


def remove(live, slug, payload=None, **headers):
    return post(live, f"/api/v1/proof-ledgers/{slug}/remove", payload or {}, **headers)


def test_the_controller_credential_creates_a_proof_ledger_named_by_code(live, credential):
    assert create(live, credential, "tcreate") == (200, {"data": {"slug": "proof-323133-tcreate-1", "created": True}})
    assert repository.get_document("proof-323133-tcreate-1")["title"] == "Anton launch proof"
    assert create(live, credential, "tcreate") == (200, {"data": {"slug": "proof-323133-tcreate-2", "created": True}})


def test_the_controller_credential_removes_a_proof_ledger_and_the_removal_is_recorded(live, credential, capsys):
    slug = create(live, credential, "tremove")[1]["data"]["slug"]
    status, reply = remove(live, slug, **{"X-Controller-Credential": credential})
    removed_at = ledger_bin.entries()[slug]
    assert (status, reply["data"]) == (200, {"slug": slug, "removed_at": removed_at})
    assert f"controller removed proof ledger {slug} at {removed_at}" in capsys.readouterr().err.splitlines()
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
        payload = {"agent": WORKER, "task": "trefused", "content": CONTENT}
        status, reply = post(live, "/api/v1/proof-ledgers", payload, **headers)
        assert (status, reply["error"]) == (
            403,
            {"code": "forbidden", "message": "Proof ledgers need the controller credential"},
        ), headers
    assert {row["slug"] for row in repository.list_summaries()} == before


def test_any_other_credential_is_refused_a_remove(live, credential):
    slug = create(live, credential, "tkept")[1]["data"]["slug"]
    for headers in other_credentials(live):
        status, reply = remove(live, slug, **headers)
        assert (status, reply["error"]["code"]) == (403, "forbidden"), headers
    assert slug not in ledger_bin.entries()


def test_a_ledger_that_is_not_a_proof_ledger_is_refused_a_remove(live, credential):
    status, reply = remove(live, SLUG, **{"X-Controller-Credential": credential})
    assert (status, reply["error"]) == (403, {"code": "forbidden", "message": "Not a proof ledger"})
    assert SLUG not in ledger_bin.entries()
    assert repository.exists(SLUG)


def test_a_remove_refuses_a_body_with_fields(live, credential):
    slug = create(live, credential, "tbody")[1]["data"]["slug"]
    status, reply = remove(live, slug, {"x": 1}, **{"X-Controller-Credential": credential})
    assert (status, reply["error"]["code"]) == (400, "schema_invalid")
    assert slug not in ledger_bin.entries()


def test_a_create_cannot_name_its_own_ledger(live, credential):
    status, reply = create(live, credential, "tnamed", slug=SLUG)
    assert (status, reply["error"]["code"]) == (400, "schema_invalid")


def test_a_create_needs_a_swarm_agent_and_task(live, credential):
    message = "a proof swarm is named from its swarm agent and task: create it from a swarm task session"
    for task, fields in (("tagent", {"agent": "someone"}), ("", {})):
        status, reply = create(live, credential, task, **fields)
        assert (status, reply["error"]) == (400, {"code": "schema_invalid", "message": message}), task


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ({"title": "", "overview": "x"}, "Content rejected: title is empty; no phases"),
        ({**CONTENT, "orchestrator": 5}, "Content rejected: orchestrator must be a string"),
    ],
)
def test_rejected_content_is_a_bad_request_and_the_server_keeps_serving(live, credential, content, message):
    status, reply = create(live, credential, "tcontent", content=content)
    assert (status, reply["error"]) == (400, {"code": "schema_invalid", "message": message})
    assert create(live, credential, "tcontent")[0] == 200


def test_a_create_whose_slug_was_taken_meanwhile_is_a_conflict(live, credential):
    slug = create(live, credential, "trace")[1]["data"]["slug"]
    with patch.object(proofs.naming, "proof_slug", return_value=slug):
        status, reply = create(live, credential, "trace")
    assert (status, reply["error"]) == (409, {"code": "ledger_exists", "message": "Ledger exists"})


def test_removing_a_missing_proof_ledger_is_not_found(live, credential):
    status, reply = remove(live, "proof-323133-t399-99", **{"X-Controller-Credential": credential})
    assert (status, reply["error"]) == (404, {"code": "ledger_missing", "message": "No such ledger"})


@pytest.mark.parametrize(("error", "extra"), [(None, {}), ("stop failed", {"swarm_error": "stop failed"})])
def test_removing_stops_the_proof_ledgers_swarm_and_reports_a_failed_stop(live, credential, error, extra):
    slug = create(live, credential, "tswarm")[1]["data"]["slug"]
    from scripts.swarm_ledger import ledger_server

    with (
        patch.object(ledger_server, "swarm_status", return_value={"state": "running"}) as swarm,
        patch.object(ledger_server, "swarm_control", return_value=(None, error)) as control,
    ):
        status, reply = remove(live, slug, **{"X-Controller-Credential": credential})
    swarm.assert_called_once_with(slug)
    control.assert_called_once_with(slug, ["stop", "--now"])
    assert (status, reply["data"]) == (200, {"slug": slug, "removed_at": ledger_bin.entries()[slug], **extra})


def client_env(live, credential, task):
    return {
        "AGENTIHOOKS_DEPLOYMENT": "compose",
        "LEDGER_URL": f"http://{live['host']}",
        "AGENTIHOOKS_CONTROLLER_CREDENTIAL": credential,
        "AGENTIHOOKS_AGENT_NAME": WORKER,
        "AGENTIHOOKS_SWARM_TASK": task,
    }


def test_the_remote_client_creates_and_removes_a_proof_ledger_with_the_controller_credential(live, credential):
    environ = client_env(live, credential, "tclient")
    with patch.object(proof_ledger.urllib.request, "urlopen", wraps=urllib.request.urlopen) as opened:
        assert proof_ledger.create(CONTENT, environ) == {"slug": "proof-323133-tclient-1", "created": True}
    assert opened.call_args.kwargs == {"timeout": 10}
    removed = proof_ledger.remove("proof-323133-tclient-1", environ)
    assert removed == {"slug": "proof-323133-tclient-1", "removed_at": ledger_bin.entries()["proof-323133-tclient-1"]}


def test_the_remote_client_needs_the_controller_credential(live, credential):
    environ = client_env(live, credential, "tnone")
    environ.pop("AGENTIHOOKS_CONTROLLER_CREDENTIAL")
    with pytest.raises(SystemExit, match="^AGENTIHOOKS_CONTROLLER_CREDENTIAL is not set$"):
        proof_ledger.create(CONTENT, environ)
    with pytest.raises(SystemExit, match="^AGENTIHOOKS_CONTROLLER_CREDENTIAL is not set$"):
        proof_ledger.remove("proof-323133-t399-1", environ)


def test_the_remote_client_reports_a_refusal(live, credential):
    with pytest.raises(SystemExit, match="^server refused: 403 Proof ledgers need the controller credential$"):
        proof_ledger.create(CONTENT, client_env(live, "wrong", "twrong"))
    with pytest.raises(SystemExit, match="^server refused: 403 Not a proof ledger$"):
        proof_ledger.remove(SLUG, client_env(live, credential, "twrong"))


def test_the_remote_client_tells_a_live_proof_ledger_from_a_binned_or_missing_one(live, credential):
    environ = client_env(live, credential, "tlive")
    slug = proof_ledger.create(CONTENT, environ)["slug"]

    assert proof_ledger.state(slug, environ) == "live"
    assert proof_ledger.state("proof-323133-tmissing-1", environ) == "missing"
    proof_ledger.remove(slug, environ)
    assert proof_ledger.state(slug, environ) == "binned"


def test_the_state_check_reports_a_refusal(live, credential):
    with pytest.raises(SystemExit, match="^server refused: 403 Missing or wrong ledger credential$"):
        proof_ledger.state(SLUG, client_env(live, "wrong", "twrong"))


def test_the_remote_client_refuses_removing_a_missing_proof_ledger(live, credential):
    with pytest.raises(SystemExit, match="^server refused: 404 No such ledger$"):
        proof_ledger.remove("proof-323133-tmissing-1", client_env(live, credential, "tmissing"))


def test_the_proof_command_creates_and_removes_through_the_client(live, credential, tmp_path, capsys):
    path = tmp_path / "content.json"
    path.write_text(json.dumps(CONTENT))
    with patch.dict(os.environ, client_env(live, credential, "tcommand")):
        proof_ledger.main(["new", "--content", str(path)])
        assert json.loads(capsys.readouterr().out) == {"slug": "proof-323133-tcommand-1", "created": True}
        run(["proof", "remove", "proof-323133-tcommand-1"])
    removed_at = ledger_bin.entries()["proof-323133-tcommand-1"]
    assert json.loads(capsys.readouterr().out) == {"slug": "proof-323133-tcommand-1", "removed_at": removed_at}


@pytest.mark.parametrize("argv", [[], ["new"]])
def test_the_proof_command_needs_an_action_and_its_content(argv):
    with pytest.raises(SystemExit) as exited:
        proof_ledger.main(argv)
    assert exited.value.code == 2
