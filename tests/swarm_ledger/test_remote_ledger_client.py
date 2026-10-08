import json
import os
import uuid
from unittest.mock import patch

import pytest

from scripts.hive import auth
from scripts.swarm_ledger import ledger, ledger_link
from scripts.swarm_ledger import ledger_authority as authority
from tests.swarm_ledger.test_ledger_authority import SLUG, WORKER, send
from tests.swarm_ledger.test_ledger_authority import live as _authority_live

pytestmark = pytest.mark.xdist_group("fakeredis")

live = _authority_live
CREDENTIAL = "hive-ledger-credential"
REMOTE = {"AGENTIHOOKS_DEPLOYMENT": "compose", "AGENTIHOOKS_SWARM": "rig-grade-swarm", "AGENTIHOOKS_AGENT_NAME": WORKER}


@pytest.fixture
def hive():
    import fakeredis

    redis = fakeredis.FakeRedis(decode_responses=True)
    redis.set(f"{auth.PREFIX}:ledger:{auth._digest(CREDENTIAL)}", "member-1")
    with patch("scripts.swarm.store.redis_client", return_value=redis):
        yield redis


def token_reply(live, **headers):
    status, data, _ = send(live, "POST", f"/api/v1/ledgers/{SLUG}/agent-token", b"{}", **headers)
    return status, json.loads(data)


def test_local_mode_reads_the_page_token_and_ignores_ledger_url(live):
    env = {"AGENTIHOOKS_SWARM": "rig-grade-swarm", "AGENTIHOOKS_AGENT_NAME": WORKER, "LEDGER_URL": "https://far:1"}
    with patch.dict(os.environ, env):
        os.environ.pop("AGENTIHOOKS_DEPLOYMENT", None)
        assert ledger.credentials(SLUG) == {
            "X-Ledger-Token": authority.agent_token(live["admin"], SLUG, WORKER),
            "X-Ledger-Agent": WORKER,
        }
        assert ledger.credentials(SLUG, service=True) == {"X-Ledger-Token": live["admin"]}
    assert ledger_link.base({"LEDGER_URL": "https://far:1", "LEDGER_HOST": "127.0.0.1", "LEDGER_PORT": "9"}) == (
        "http://127.0.0.1:9"
    )


def test_non_local_mode_reaches_the_ledger_at_ledger_url():
    assert ledger_link.base({"AGENTIHOOKS_DEPLOYMENT": "distributed", "LEDGER_URL": "https://hub.example/"}) == (
        "https://hub.example"
    )


def test_a_remote_client_never_reads_the_page_token():
    def refuse(_page):
        raise AssertionError("a remote client read the page token")

    with patch.dict(os.environ, {**REMOTE, "AGENTIHOOKS_LEDGER_AGENT_TOKEN": "launch-token"}):
        with patch.object(ledger.core, "read_token", refuse):
            assert ledger.credentials(SLUG) == {"X-Ledger-Token": "launch-token", "X-Ledger-Agent": WORKER}


def test_the_server_derives_the_agent_token_for_a_hive_credential(live, hive):
    status, reply = token_reply(live, **{"X-Hive-Credential": CREDENTIAL, "X-Ledger-Agent": WORKER})
    assert status == 200
    assert reply["data"] == {"agent": WORKER, "token": authority.agent_token(live["admin"], SLUG, WORKER)}


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Ledger-Agent": WORKER},
        {"X-Hive-Credential": "wrong", "X-Ledger-Agent": WORKER},
        {"X-Hive-Credential": CREDENTIAL},
    ],
)
def test_a_client_without_a_hive_credential_and_agent_is_refused(live, hive, headers):
    status, reply = token_reply(live, **headers)
    assert (status, reply["error"]["code"]) == (403, "forbidden")


def test_a_remote_client_with_a_hive_credential_joins_and_comments(live, hive):
    ledger.launch_token.cache_clear()
    with patch.dict(os.environ, {**REMOTE, "AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL": CREDENTIAL}):
        os.environ.pop("AGENTIHOOKS_LEDGER_AGENT_TOKEN", None)
        with patch.object(ledger.core, "read_token", side_effect=AssertionError("page token read")):
            join = {"op": "join", "id": uuid.uuid4().hex, "by": WORKER}
            say = {"op": "add", "id": uuid.uuid4().hex, "by": WORKER, "thread": "chat", "text": "Remote hello"}
            reply = ledger.request(SLUG, [join, say])
    assert not {join["id"], say["id"]} & set(reply.get("rejected") or [])
    assert any(entry.get("text") == "Remote hello" for entry in reply["chat"])


def test_a_remote_client_without_a_credential_is_refused(live, hive):
    ledger.launch_token.cache_clear()
    with patch.dict(os.environ, REMOTE):
        os.environ.pop("AGENTIHOOKS_LEDGER_AGENT_TOKEN", None)
        os.environ.pop("AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL", None)
        with pytest.raises(SystemExit, match="hive credential"):
            ledger.credentials(SLUG)
