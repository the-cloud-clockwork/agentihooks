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
from scripts.swarm_ledger import ledger, ledger_hook, ledger_link
from scripts.swarm_ledger import ledger_authority as authority
from scripts.swarm_ledger.api import routes
from scripts.swarm_ledger.api.errors import APIError
from tests.swarm_ledger.test_ledger_authority import SLUG, WORKER, send
from tests.swarm_ledger.test_ledger_authority import live as _authority_live

pytestmark = pytest.mark.xdist_group("fakeredis")

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
        with pytest.raises(SystemExit, match="LEDGER_URL"):
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
    with exits("a remote ledger client needs LEDGER_URL, the address of the hive ledger server"):
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
            "a remote ledger client needs AGENTIHOOKS_LEDGER_AGENT_TOKEN or the hive credential "
            "AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL from agentihooks hive join"
        ):
            ledger.credentials(SLUG)
        os.environ.pop("AGENTIHOOKS_AGENT_NAME")
        with exits("a remote ledger client needs a pinned agent identity; the operator credential stays on its host"):
            ledger.credentials(SLUG)


def test_a_refused_launch_token_fetch_sends_both_headers_and_names_the_status():
    client = Mock()
    client.return_value.request.side_effect = urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
    with (
        patch.dict(os.environ, {**REMOTE, "AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL": CREDENTIAL}),
        patch.object(ledger, "BASE", ""),
        patch("scripts.swarm_ledger.api.client.ResourceClient", client),
        exits("the ledger server refused the hive credential: 403"),
    ):
        ledger.launch_token(SLUG, WORKER)
    client.assert_called_once_with("https://hub.example", {"X-Hive-Credential": CREDENTIAL, "X-Ledger-Agent": WORKER})
    client.return_value.request.assert_called_once_with(SLUG, "agent-token", {})


def test_the_agent_token_route_answers_or_names_what_is_missing():
    server = SimpleNamespace(
        authority=SimpleNamespace(
            hive_member=lambda credential: "member-1" if credential == CREDENTIAL else None,
            agent_token=authority.agent_token,
        ),
        core=SimpleNamespace(read_token=lambda page: f"admin-of-{page}"),
        repository=SimpleNamespace(read_page=lambda slug: slug),
    )
    for headers, message in (
        ({"X-Hive-Credential": CREDENTIAL}, "An agent token names its agent in X-Ledger-Agent"),
        ({"X-Hive-Credential": "wrong", "X-Ledger-Agent": WORKER}, "Missing or wrong hive credential"),
        ({"X-Ledger-Agent": WORKER}, "Missing or wrong hive credential"),
    ):
        with pytest.raises(APIError) as refused:
            routes.agent_token(SimpleNamespace(headers=headers), server, SLUG)
        assert (refused.value.status, refused.value.code, str(refused.value)) == (403, "forbidden", message)
    granted = SimpleNamespace(headers={"X-Hive-Credential": CREDENTIAL, "X-Ledger-Agent": WORKER})
    assert routes.agent_token(granted, server, SLUG) == {
        "data": {"agent": WORKER, "token": authority.agent_token(f"admin-of-{SLUG}", SLUG, WORKER)}
    }
