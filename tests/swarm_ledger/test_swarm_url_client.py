import http.server
import json
import threading
import urllib.request
from unittest.mock import patch

import pytest

from scripts.swarm_ledger import ledger, ledger_hook, ledger_link
from scripts.swarm_ledger import ledger_authority as authority
from scripts.swarm_ledger.api.client import ResourceClient

SLUG = "rig-grade-swarm"
WORKER = "engineer@323133-0001"
SEED = "seed-one"
PARTS = ("id-one.access", "part-two")
ACCESS = dict(zip(("SWARM_ACCESS_CLIENT_ID", "SWARM_ACCESS_CLIENT_SECRET"), PARTS))
SENT = {**dict(zip(("CF-Access-Client-Id", "CF-Access-Client-Secret"), PARTS)), "User-Agent": "agentihooks-swarm"}


@pytest.fixture
def heard():
    seen = []

    class Echo(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers)
            body = json.dumps({"dir": "/srv/ledgers", "data": {}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.do_GET()

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", seen
    httpd.shutdown()
    httpd.server_close()


def test_swarm_url_makes_the_client_remote_and_is_its_base():
    assert ledger_link.remote({"SWARM_URL": "https://swarm.example/"}) is True
    assert ledger_link.remote({"SWARM_URL": ""}) is False
    assert ledger_link.remote({}) is False
    assert ledger_link.base({"SWARM_URL": "https://swarm.example/"}) == "https://swarm.example"
    both = {"SWARM_URL": "https://swarm.example", "LEDGER_URL": "https://hub.example", "AGENTIHOOKS_DEPLOYMENT": "x"}
    assert ledger_link.base(both) == "https://swarm.example"
    assert ledger_link.base({"AGENTIHOOKS_DEPLOYMENT": "x", "LEDGER_URL": "https://hub.example/"}) == (
        "https://hub.example"
    )


def test_a_remote_client_without_an_address_names_swarm_url():
    with pytest.raises(SystemExit) as exc:
        ledger_link.base({"AGENTIHOOKS_DEPLOYMENT": "compose"})
    assert str(exc.value) == "a remote ledger client needs SWARM_URL, the address of the hosted ledger server"


def test_access_headers_need_both_service_token_parts():
    first, second = ACCESS.items()
    assert ledger_link.access_headers(ACCESS) == SENT
    assert ledger_link.access_headers(dict([first])) == {}
    assert ledger_link.access_headers(dict([second])) == {}
    assert ledger_link.access_headers({}) == {}


def test_every_api_request_carries_the_access_headers(heard, monkeypatch):
    url, seen = heard
    for key, value in ACCESS.items():
        monkeypatch.setenv(key, value)
    ResourceClient(url, {"X-Ledger-Token": "t", "X-Ledger-Agent": WORKER}).request(SLUG, "metadata")
    assert {key: seen[0].get(key) for key in (*SENT, "X-Ledger-Token", "X-Ledger-Agent")} == {
        **SENT,
        "X-Ledger-Token": "t",
        "X-Ledger-Agent": WORKER,
    }


def test_a_request_without_access_settings_sends_no_access_headers(heard, monkeypatch):
    url, seen = heard
    for key in ACCESS:
        monkeypatch.delenv(key, raising=False)
    ResourceClient(url, {"X-Ledger-Token": "t"}).request(SLUG, "metadata")
    assert {key for key in seen[0] if key.lower().startswith("cf-access")} == set()
    assert seen[0].get("User-Agent").startswith("Python-urllib/")


def test_the_health_probe_carries_the_access_headers(heard, monkeypatch):
    url, seen = heard
    for key, value in ACCESS.items():
        monkeypatch.setenv(key, value)
    assert ledger_link.serving(url=url) == "/srv/ledgers"
    assert {key: seen[0].get(key) for key in SENT} == SENT


def test_a_pinned_agent_derives_its_token_from_the_server_secret(monkeypatch):
    env = {"SWARM_URL": "https://swarm.example", "AGENTIHOOKS_SWARM": SLUG, "AGENTIHOOKS_AGENT_NAME": WORKER}
    with patch.dict("os.environ", env):
        monkeypatch.setenv("SWARM_SECRET", SEED)
        for key in ("AGENTIHOOKS_LEDGER_AGENT_TOKEN", "AGENTIHOOKS_CONTROLLER_CREDENTIAL", "AGENTIHOOKS_DEPLOYMENT"):
            monkeypatch.delenv(key, raising=False)
        assert ledger.credentials(SLUG) == {
            "X-Ledger-Token": authority.agent_token(SEED, SLUG, WORKER),
            "X-Ledger-Agent": WORKER,
        }
        monkeypatch.setenv("AGENTIHOOKS_LEDGER_AGENT_TOKEN", "given")
        assert ledger.credentials(SLUG) == {"X-Ledger-Token": "given", "X-Ledger-Agent": WORKER}


def test_swarm_url_never_starts_a_local_server(monkeypatch):
    monkeypatch.setenv("SWARM_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("AGENTIHOOKS_LEDGER_AGENT_TOKEN", "given")
    monkeypatch.setenv("AGENTIHOOKS_SWARM", SLUG)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", WORKER)
    monkeypatch.delenv("LEDGER_AUTOSTART", raising=False)
    monkeypatch.setattr(ledger, "BASE", "")
    monkeypatch.setattr(ledger, "REMOTE_READ_PAUSE", 0)

    def started(*args, **kwargs):
        raise AssertionError(f"a local ledger server was started: {args}")

    monkeypatch.setattr(ledger.subprocess, "run", started)
    monkeypatch.setattr(ledger_hook.subprocess, "Popen", started)
    with pytest.raises(SystemExit) as exc:
        ledger.call(SLUG)
    assert str(exc.value) == (
        "ledger server not answering on http://127.0.0.1:9: <urlopen error [Errno 111] Connection refused>"
    )
    assert ledger_hook.serve_ledgers() is None


def test_every_ledger_transport_request_carries_the_access_headers(heard):
    url, seen = heard
    request = urllib.request.Request(f"{url}/healthz", headers={"Accept": "text/event-stream"})
    with ledger_link.urlopen(request, 1, ACCESS):
        pass
    assert {key: seen[0].get(key) for key in (*SENT, "Accept")} == {**SENT, "Accept": "text/event-stream"}


def test_a_proof_ledger_call_carries_the_access_headers(heard):
    from scripts.swarm_ledger import proof_ledger

    url, seen = heard
    environ = {"SWARM_URL": url, "AGENTIHOOKS_CONTROLLER_CREDENTIAL": "controller", **ACCESS}
    assert proof_ledger.call(f"ledgers/{SLUG}/metadata", None, environ) == {}
    assert {key: seen[0].get(key) for key in (*SENT, "X-Controller-Credential", "Content-Type")} == {
        **SENT,
        "X-Controller-Credential": "controller",
        "Content-Type": "application/json",
    }


def test_a_proof_control_call_carries_the_access_headers(heard):
    from scripts.swarm_ledger import proof_ledger

    url, seen = heard
    environ = {"SWARM_URL": url, **ACCESS}
    assert proof_ledger._control_call(SLUG, "controls", {}, {"X-Proof-Credential": "proof"}, environ) == {}
    assert {key: seen[0].get(key) for key in (*SENT, "X-Proof-Credential", "Content-Type")} == {
        **SENT,
        "X-Proof-Credential": "proof",
        "Content-Type": "application/json",
    }


def test_an_upload_carries_the_access_headers(heard, monkeypatch, tmp_path):
    url, seen = heard
    monkeypatch.setenv("SWARM_URL", url)
    monkeypatch.setenv("AGENTIHOOKS_LEDGER_AGENT_TOKEN", "given")
    monkeypatch.setenv("AGENTIHOOKS_SWARM", SLUG)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", WORKER)
    monkeypatch.delenv("AGENTIHOOKS_CONTROLLER_CREDENTIAL", raising=False)
    for key, value in ACCESS.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(ledger, "BASE", "")
    sent = tmp_path / "plan.md"
    sent.write_text("plan")
    assert ledger.upload(SLUG, WORKER, str(sent), "artifacts", {}) == {"dir": "/srv/ledgers", "data": {}}
    assert {key: seen[0].get(key) for key in (*SENT, "X-Ledger-Token")} == {**SENT, "X-Ledger-Token": "given"}
