import json
import os
import re
import subprocess
import sys
import threading
import urllib.error
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from scripts.hive import auth
from scripts.swarm.store import AgentRecord, RedisStore
from scripts.swarm_ledger import ledger, ledger_hook, ledger_link
from scripts.swarm_ledger import ledger_authority as authority
from scripts.swarm_ledger.api import routes
from scripts.swarm_ledger.api.errors import APIError
from tests.swarm_ledger.api_fakes import as_services
from tests.swarm_ledger.test_ledger_authority import SLUG, WORKER, send
from tests.swarm_ledger.test_ledger_authority import live as _authority_live

live = _authority_live
ROOT = Path(__file__).resolve().parents[2]
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
    redis.hset(f"{auth.PREFIX}:member:member-1", "ledger", auth._digest(CREDENTIAL))
    store = RedisStore(redis)
    store.names.adopt(SLUG, "323133", "", "")
    redis.hset(store.names.key("name", WORKER), mapping={"swarm": SLUG, "code": "323133"})
    store.start_execution(SLUG, AgentRecord(WORKER, "eng", "proof", seat=f"eng-1@{SLUG}", hive="member-1"))
    with patch("scripts.swarm.store.redis_client", return_value=redis):
        yield redis


def token_reply(live, **headers):
    status, data, _ = send(live, "POST", f"/api/v1/ledgers/{SLUG}/agent-token", b"{}", **headers)
    return status, json.loads(data)


def server_only(read_token):
    def read(slug):
        assert threading.current_thread() is not threading.main_thread(), "the remote client read the page token"
        return read_token(slug)

    return read


def test_explicit_local_mode_reads_the_page_token_and_ignores_ledger_url(live):
    env = {
        "AGENTIHOOKS_DEPLOYMENT": "local",
        "AGENTIHOOKS_SWARM": "rig-grade-swarm",
        "AGENTIHOOKS_AGENT_NAME": WORKER,
        "LEDGER_URL": "https://far:1",
    }
    with patch.dict(os.environ, env):
        assert ledger.credentials(SLUG) == {
            "X-Ledger-Token": authority.agent_token(live["admin"], SLUG, WORKER),
            "X-Ledger-Agent": WORKER,
        }
        assert ledger.credentials(SLUG, service=True) == {"X-Ledger-Token": live["admin"]}
    local = {
        "AGENTIHOOKS_DEPLOYMENT": "local",
        "LEDGER_URL": "https://far:1",
        "LEDGER_DIR": "/elsewhere",
        "LEDGER_HOST": "127.0.0.1",
        "LEDGER_PORT": "9",
    }
    assert ledger_link.remote(local) is False
    assert ledger_link.remote({**local, "SWARM_URL": "https://swarm.example"}) is True
    assert ledger_link.base(local) == "http://127.0.0.1:9"


def test_a_hub_address_without_a_deployment_mode_is_a_remote_client_that_never_reads_the_page_token():
    def refuse(_slug):
        raise AssertionError("a remote client read the page token")

    hub = {key: value for key, value in REMOTE.items() if key != "AGENTIHOOKS_DEPLOYMENT"}
    assert ledger_link.remote(hub) is True
    assert ledger_link.remote({}) is False
    assert ledger_link.base(hub) == "https://hub.example"
    with patch.dict(os.environ, {**hub, "AGENTIHOOKS_LEDGER_AGENT_TOKEN": "launch-token"}):
        os.environ.pop("AGENTIHOOKS_DEPLOYMENT", None)
        os.environ.pop("SWARM_URL", None)
        with patch.object(ledger.repository, "token", refuse):
            assert ledger.credentials(SLUG) == {"X-Ledger-Token": "launch-token", "X-Ledger-Agent": WORKER}


def test_non_local_mode_reaches_the_ledger_at_ledger_url():
    assert ledger_link.base({"AGENTIHOOKS_DEPLOYMENT": "distributed", "LEDGER_URL": "https://hub.example/"}) == (
        "https://hub.example"
    )


def test_a_remote_client_never_reads_the_page_token():
    def refuse(_slug):
        raise AssertionError("a remote client read the page token")

    with patch.dict(os.environ, {**REMOTE, "AGENTIHOOKS_LEDGER_AGENT_TOKEN": "launch-token"}):
        with patch.object(ledger.repository, "token", refuse):
            assert ledger.credentials(SLUG) == {"X-Ledger-Token": "launch-token", "X-Ledger-Agent": WORKER}


def test_the_server_derives_the_agent_token_for_a_hive_credential(live, hive):
    status, reply = token_reply(live, **{"X-Hive-Credential": CREDENTIAL, "X-Ledger-Agent": WORKER})
    assert status == 200
    assert reply["data"]["agent"] == WORKER
    assert authority.principal(live["admin"], SLUG, reply["data"]["token"], WORKER) == WORKER


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
        with patch.object(ledger.repository, "token", server_only(ledger.repository.token)):
            join = {"op": "join", "id": uuid.uuid4().hex, "by": WORKER}
            say = {
                "op": "add",
                "id": uuid.uuid4().hex,
                "by": WORKER,
                "thread": "chat",
                "text": "Remote hello",
                "to": "operator",
            }
            reply = ledger.request(SLUG, [join, say])
            state = ledger.request(SLUG)
    assert not {join["id"], say["id"]} & set(reply["rejected"])
    assert WORKER in state["_meta"]["members"]
    assert any(entry.get("text") == "Remote hello" for entry in state["chat"])


def test_remote_swarm_state_uses_the_callers_agent_credential(live, hive):
    from scripts.swarm.ledger_client import LedgerClient

    with patch.dict(os.environ, {**REMOTE, "AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL": CREDENTIAL}):
        os.environ.pop("AGENTIHOOKS_LEDGER_AGENT_TOKEN", None)
        with patch.object(ledger.repository, "token", server_only(ledger.repository.token)):
            ledger.request(SLUG, [{"op": "join", "id": uuid.uuid4().hex, "by": WORKER}])
            state = LedgerClient().state(SLUG)
    assert WORKER in state["_meta"]["members"]


@pytest.fixture
def hosted_controls(live, hive, monkeypatch):
    from scripts.swarm import cli
    from scripts.swarm.ledger_client import LedgerClient
    from scripts.swarm.store import SwarmConfig
    from tests.swarm_ledger.test_ledger_authority import MASTER

    store = RedisStore(hive)
    store.create(SwarmConfig(SLUG, "/repo", max_eng=2, max_ci=1, state="running"))
    store.put_agent(SLUG, AgentRecord(MASTER, "master", "master", seat=f"master@{SLUG}"))
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "compose")
    monkeypatch.setenv("AGENTIHOOKS_SWARM", SLUG)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", MASTER)
    monkeypatch.setenv("AGENTIHOOKS_LEDGER_AGENT_TOKEN", authority.agent_token(live["admin"], SLUG, MASTER))
    monkeypatch.delenv("AGENTIHOOKS_CONTROLLER_CREDENTIAL", raising=False)
    monkeypatch.delenv("SWARM_SECRET", raising=False)
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setattr(cli, "run_tick", lambda *args: [])
    monkeypatch.setattr(cli, "_snapshot", lambda *args: None)
    monkeypatch.setattr(cli.timer, "ensure", lambda *args: True)
    monkeypatch.setattr(ledger.repository, "token", server_only(ledger.repository.token))
    LedgerClient().join(SLUG, MASTER, "orchestrator")
    return store


def test_pinned_master_controls_cross_the_hosted_credential_boundary(hosted_controls):
    from scripts.swarm import cli
    from scripts.swarm.ledger_client import LedgerClient
    from tests.swarm_ledger.test_ledger_authority import MASTER

    store = hosted_controls
    before = len(LedgerClient().state(SLUG)["notifications"])
    for arguments, state in [
        (["pause"], "paused"),
        (["start"], "running"),
        (["set", "max-eng-agents=3"], "running"),
        (["stop"], "stopping"),
    ]:
        assert cli.main([SLUG, *arguments]) == 0
        assert store.config(SLUG).state == state
    assert store.config(SLUG).max_eng == 3
    notices = LedgerClient().state(SLUG)["notifications"][before:]
    assert len(notices) == 4
    assert all(notice["by"] == MASTER for notice in notices)
    with exits("a remote ledger client cannot make service writes; the operator credential stays on its host"):
        ledger.credentials(SLUG, service=True)


@pytest.mark.parametrize("arguments", [["pause"], ["start"], ["stop"], ["set", "max-eng-agents=3"]])
def test_hosted_controls_still_refuse_a_nonmaster(hosted_controls, monkeypatch, capsys, arguments):
    from scripts.swarm import cli

    store = hosted_controls
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", WORKER)
    before = store.config(SLUG)
    assert cli.main([SLUG, *arguments]) == 1
    assert capsys.readouterr().err == (
        f"swarm: only the operator or the master of swarm {SLUG} uses its swarm controls, and {WORKER} is neither\n"
    )
    assert store.config(SLUG) == before


def test_a_remote_client_without_a_credential_is_refused(live, hive):
    with patch.dict(os.environ, REMOTE):
        os.environ.pop("AGENTIHOOKS_LEDGER_AGENT_TOKEN", None)
        os.environ.pop("AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL", None)
        with pytest.raises(SystemExit, match="hive credential"):
            ledger.credentials(SLUG)


def test_a_remote_client_without_ledger_url_imports_and_is_refused_at_request_time():
    env = {key: value for key, value in os.environ.items() if key != "LEDGER_URL"}
    env.update(AGENTIHOOKS_DEPLOYMENT="compose", PYTHONPATH=str(ROOT))
    imported = subprocess.run(
        [sys.executable, "-c", "from scripts.swarm_ledger import ledger; print(repr(ledger.BASE))"],
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert (imported.returncode, imported.stdout.strip()) == (0, "''")
    with patch.dict(os.environ, {"AGENTIHOOKS_DEPLOYMENT": "compose"}), patch.object(ledger, "BASE", ""):
        os.environ.pop("LEDGER_URL", None)
        with pytest.raises(SystemExit, match="SWARM_URL"):
            ledger.base()


def test_a_remote_client_refuses_service_writes():
    with patch.dict(os.environ, REMOTE):
        with pytest.raises(SystemExit, match="service writes"):
            ledger.credentials(SLUG, service=True)


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


def exits(message):
    return pytest.raises(SystemExit, match=f"^{re.escape(message)}$")


def test_remote_refusals_name_what_is_missing():
    with exits("a remote ledger client needs SWARM_URL, the address of the hosted ledger server"):
        ledger_link.base({"AGENTIHOOKS_DEPLOYMENT": "compose"})
    assert ledger_link.base({"AGENTIHOOKS_DEPLOYMENT": "compose", "LEDGER_URL": "https://hub.example/X/"}) == (
        "https://hub.example/X"
    )
    with patch.dict(os.environ, REMOTE):
        os.environ.pop("AGENTIHOOKS_LEDGER_AGENT_TOKEN", None)
        os.environ.pop("AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL", None)
        with exits("a remote ledger client cannot make service writes; the operator credential stays on its host"):
            ledger.credentials(SLUG, service=True)
        with exits(
            "unauthenticated: a remote ledger client needs AGENTIHOOKS_LEDGER_AGENT_TOKEN or the hive credential "
            "AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL from agentihooks hive add"
        ):
            ledger.credentials(SLUG)
        os.environ.pop("AGENTIHOOKS_AGENT_NAME")
        with exits(
            "unauthenticated: a remote ledger client needs a pinned agent identity; "
            "the operator credential stays on its host"
        ):
            ledger.credentials(SLUG)


def test_a_refused_launch_token_fetch_sends_both_headers_and_names_the_status():
    client = Mock()
    client.return_value.request.side_effect = urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
    with (
        patch.dict(os.environ, {**REMOTE, "AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL": CREDENTIAL}),
        patch.object(ledger, "BASE", ""),
        patch("scripts.swarm_ledger.api.client.ResourceClient", client),
        exits("unauthenticated: the ledger server refused the hive credential: 403"),
    ):
        ledger.launch_token(SLUG, WORKER)
    client.assert_called_once_with(
        "https://hub.example", {"X-Hive-Credential": CREDENTIAL, "X-Ledger-Agent": WORKER}, ledger.REQUEST_TIMEOUT
    )
    client.return_value.request.assert_called_once_with(SLUG, "agent-token", {})


def test_the_agent_token_route_answers_or_names_what_is_missing():
    server = SimpleNamespace(
        authority=SimpleNamespace(
            hive_member=lambda credential: "member-1" if credential == CREDENTIAL else None,
            hive_agent=lambda member, slug, name: (member, slug, name) == ("member-1", SLUG, WORKER),
            hive_agent_token=lambda member, slug, name: "issued-token",
        ),
        repository=SimpleNamespace(token=lambda slug: f"admin-of-{slug}"),
    )
    for headers, message in (
        ({"X-Hive-Credential": CREDENTIAL}, "An agent token names its agent in X-Ledger-Agent"),
        ({"X-Hive-Credential": "wrong", "X-Ledger-Agent": WORKER}, "Missing or wrong hive credential"),
        ({"X-Ledger-Agent": WORKER}, "Missing or wrong hive credential"),
    ):
        with pytest.raises(APIError) as refused:
            routes.agent_token(SimpleNamespace(headers=headers), as_services(server), SLUG)
        assert (refused.value.status, refused.value.code, str(refused.value)) == (403, "forbidden", message)
    granted = SimpleNamespace(headers={"X-Hive-Credential": CREDENTIAL, "X-Ledger-Agent": WORKER})
    assert routes.agent_token(granted, as_services(server), SLUG) == {
        "data": {"agent": WORKER, "token": "issued-token"}
    }
