import json
import os
from unittest.mock import patch

import pytest

from scripts.hive import auth, cli
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.store import SwarmError
from scripts.swarm_ledger import ledger
from scripts.swarm_ledger import ledger_authority as authority
from tests.swarm_ledger.test_ledger_authority import SLUG, WORKER, send
from tests.swarm_ledger.test_ledger_authority import live as _authority_live
from tests.swarm_ledger.test_remote_ledger_client import REMOTE, server_only

pytestmark = pytest.mark.xdist_group("fakeredis")

live = _authority_live
CONTROLLER = {"AGENTIHOOKS_DEPLOYMENT": "compose", "LEDGER_URL": "https://hub.example"}
UNPINNED = ("AGENTIHOOKS_AGENT_NAME", "AGENTIHOOKS_SWARM", "AGENTIHOOKS_LEDGER_AGENT_TOKEN")


@pytest.fixture
def hive():
    import fakeredis

    redis = fakeredis.FakeRedis(decode_responses=True)
    with patch("scripts.swarm.store.redis_client", return_value=redis):
        yield redis


@pytest.fixture
def controller_env(hive):
    credential = auth.issue_controller(hive)
    with patch.dict(os.environ, {**CONTROLLER, "AGENTIHOOKS_CONTROLLER_CREDENTIAL": credential}):
        for name in UNPINNED:
            os.environ.pop(name, None)
        yield credential


def test_an_issued_controller_credential_is_stored_only_as_its_digest_and_checks(hive):
    credential = auth.issue_controller(hive)
    assert credential not in {hive.get(key) for key in hive.keys()}
    assert auth.controller(hive, credential)
    assert not auth.controller(hive, "wrong")
    assert not auth.controller(hive, "")


def test_reissuing_the_controller_credential_retires_the_old_one(hive):
    old = auth.issue_controller(hive)
    new = auth.issue_controller(hive)
    assert old != new
    assert not auth.controller(hive, old)
    assert auth.controller(hive, new)


def test_no_controller_credential_is_valid_before_stack_setup(hive):
    assert not auth.controller(hive, "anything")
    assert not authority.controller("anything")


def test_the_hive_controller_command_prints_a_credential_the_server_accepts(hive, capsys):
    with patch.object(cli, "redis_client", return_value=hive):
        assert cli.main(["controller"]) == 0
    assert authority.controller(capsys.readouterr().out.strip())


def test_a_remote_controller_presents_its_credential_for_service_writes_and_reads(controller_env):
    headers = {"X-Controller-Credential": controller_env}
    assert ledger.credentials(SLUG, service=True) == headers
    assert ledger.credentials(SLUG) == headers


def test_a_pinned_agent_keeps_its_agent_token_for_its_own_writes(controller_env):
    with patch.dict(os.environ, {**REMOTE, "AGENTIHOOKS_LEDGER_AGENT_TOKEN": "launch-token"}):
        assert ledger.credentials(SLUG) == {"X-Ledger-Token": "launch-token", "X-Ledger-Agent": WORKER}


def test_the_controller_writes_and_reads_a_remote_ledger_as_the_service(live, controller_env):
    with patch.object(ledger.core, "read_token", server_only(ledger.core.read_token)):
        client = LedgerClient()
        client.notify(SLUG, "Controller hello")
        client.followup(SLUG, "Controller follow up")
        assert isinstance(client.tasks(SLUG), list)
        state = client.state(SLUG)
    assert any(entry.get("text") == "Controller hello" for entry in state["chat"])
    assert any(item.get("text") == "Controller follow up" for item in state["followups"])


@pytest.mark.parametrize("credential", ["wrong", ""])
def test_the_server_refuses_a_wrong_controller_credential(live, controller_env, credential):
    status, data, _ = send(live, "GET", f"/api/v1/ledgers/{SLUG}/tasks", **{"X-Controller-Credential": credential})
    assert (status, json.loads(data)["error"]["code"]) == (403, "forbidden")


def test_an_agent_client_without_the_controller_credential_still_cannot_make_service_writes(live, hive):
    auth.issue_controller(hive)
    with patch.dict(os.environ, {**REMOTE, "AGENTIHOOKS_LEDGER_AGENT_TOKEN": "launch-token"}):
        os.environ.pop("AGENTIHOOKS_CONTROLLER_CREDENTIAL", None)
        with pytest.raises(SystemExit, match="service writes"):
            ledger.credentials(SLUG, service=True)
        with pytest.raises(SwarmError, match="service writes"):
            LedgerClient().notify(SLUG, "Agent posing as the swarm")


def test_an_agent_token_cannot_write_as_the_swarm_beside_a_controller_credential(live, hive):
    auth.issue_controller(hive)
    token = authority.agent_token(live["admin"], SLUG, WORKER)
    posing = {"op": "add", "id": "a1", "by": "swarm", "thread": "chat", "text": "posing"}
    with patch.dict(os.environ, {**REMOTE, "AGENTIHOOKS_LEDGER_AGENT_TOKEN": token}):
        os.environ.pop("AGENTIHOOKS_CONTROLLER_CREDENTIAL", None)
        reply = ledger.request(SLUG, [posing])
    assert "a1" in reply["rejected"]
