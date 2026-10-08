import json
import os
import threading
import uuid
from unittest.mock import Mock, patch

import pytest

from scripts.hive import auth
from scripts.swarm_ledger import ledger, ledger_hook, ledger_link
from scripts.swarm_ledger import ledger_authority as authority
from tests.swarm_ledger.test_ledger_authority import SLUG, WORKER, send
from tests.swarm_ledger.test_ledger_authority import live as _authority_live

pytestmark = pytest.mark.xdist_group("fakeredis")

live = _authority_live
CREDENTIAL = "hive-ledger-credential"
REMOTE = {
    "AGENTIHOOKS_DEPLOYMENT": "compose",
    "AGENTIHOOKS_SWARM": "rig-grade-swarm",
    "AGENTIHOOKS_AGENT_NAME": WORKER,
    "LEDGER_URL": "https://hub.example",
}


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


def server_only(read_token):
    def read(page):
        assert threading.current_thread() is not threading.main_thread(), "the remote client read the page token"
        return read_token(page)

    return read


def test_local_mode_reads_the_page_token_and_ignores_ledger_url(live):
    env = {"AGENTIHOOKS_SWARM": "rig-grade-swarm", "AGENTIHOOKS_AGENT_NAME": WORKER, "LEDGER_URL": "https://far:1"}
    with patch.dict(os.environ, env):
        os.environ.pop("AGENTIHOOKS_DEPLOYMENT", None)
        assert ledger.credentials(SLUG) == {
            "X-Ledger-Token": authority.agent_token(live["admin"], SLUG, WORKER),
            "X-Ledger-Agent": WORKER,
        }
        assert ledger.credentials(SLUG, service=True) == {"X-Ledger-Token": live["admin"]}
    local = {"LEDGER_URL": "https://far:1", "LEDGER_DIR": "/elsewhere", "LEDGER_HOST": "127.0.0.1", "LEDGER_PORT": "9"}
    assert ledger_link.base(local) == "http://127.0.0.1:9"


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
        {"X-Hive-Credential": CREDENTIAL, "X-Ledger-Agent": WORKER, "Host": "evil.example"},
    ],
)
def test_a_client_without_a_hive_credential_and_agent_is_refused(live, hive, headers):
    status, reply = token_reply(live, **headers)
    assert (status, reply["error"]["code"]) == (403, "forbidden")


def test_a_remote_client_with_a_hive_credential_joins_and_comments(live, hive):
    with patch.dict(os.environ, {**REMOTE, "AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL": CREDENTIAL}):
        os.environ.pop("AGENTIHOOKS_LEDGER_AGENT_TOKEN", None)
        with patch.object(ledger.core, "read_token", server_only(ledger.core.read_token)):
            join = {"op": "join", "id": uuid.uuid4().hex, "by": WORKER}
            say = {"op": "add", "id": uuid.uuid4().hex, "by": WORKER, "thread": "chat", "text": "Remote hello"}
            reply = ledger.request(SLUG, [join, say])
            state = ledger.request(SLUG)
    assert not {join["id"], say["id"]} & set(reply["rejected"])
    assert WORKER in state["_meta"]["members"]
    assert any(entry.get("text") == "Remote hello" for entry in state["chat"])


def test_a_remote_client_without_a_credential_is_refused(live, hive):
    with patch.dict(os.environ, REMOTE):
        os.environ.pop("AGENTIHOOKS_LEDGER_AGENT_TOKEN", None)
        os.environ.pop("AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL", None)
        with pytest.raises(SystemExit, match="hive credential"):
            ledger.credentials(SLUG)


def test_a_remote_client_without_ledger_url_is_refused():
    with patch.dict(os.environ, {**REMOTE, "AGENTIHOOKS_LEDGER_AGENT_TOKEN": "launch-token"}):
        os.environ.pop("LEDGER_URL")
        with pytest.raises(SystemExit, match="LEDGER_URL"):
            ledger.credentials(SLUG)


def test_a_remote_client_never_starts_a_local_ledger_server():
    with (
        patch.dict(os.environ, {**REMOTE, "AGENTIHOOKS_LEDGER_AGENT_TOKEN": "launch-token"}),
        patch.object(ledger, "request", side_effect=OSError("down")),
        patch.object(ledger.repository, "exists", return_value=True),
        patch.object(ledger.subprocess, "run") as run,
        pytest.raises(SystemExit, match="not answering"),
    ):
        os.environ.pop("LEDGER_AUTOSTART", None)
        ledger.call(SLUG)
    run.assert_not_called()


def test_a_remote_session_start_starts_no_ledger_server(tmp_path, monkeypatch):
    (tmp_path / "remote.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ledger_hook, "LEDGER_DIR", tmp_path)
    monkeypatch.delenv("LEDGER_AUTOSTART", raising=False)
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "distributed")
    monkeypatch.setattr(ledger_hook.socket, "create_connection", Mock(side_effect=OSError("closed")))
    monkeypatch.setattr(ledger_hook.subprocess, "Popen", Mock())
    ledger_hook.serve_ledgers()
    ledger_hook.subprocess.Popen.assert_not_called()
