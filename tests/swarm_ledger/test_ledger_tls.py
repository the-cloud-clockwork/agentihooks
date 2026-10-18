import io
import os
import subprocess
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.swarm_ledger import ledger_link, ledger_server, proof_ledger
from scripts.swarm_ledger.api import client as api_client
from tests.swarm_ledger import legacy_page
from tests.swarm_ledger.test_ledger_authority import SLUG, Burst, core, new_ledger

pytestmark = pytest.mark.xdist_group("fakeredis")

CREDENTIALS = [
    {"X-Controller-Credential": "controller"},
    {"X-Hive-Credential": "hive", "X-Ledger-Agent": "engineer@323133-0256"},
    {"X-Ledger-Token": "token"},
]


@pytest.fixture
def cert(tmp_path):
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost",
            "-keyout",
            tmp_path / "key.pem",
            "-out",
            tmp_path / "cert.pem",
        ],
        check=True,
        capture_output=True,
    )
    return str(tmp_path / "cert.pem"), str(tmp_path / "key.pem")


def opened(monkeypatch):
    seen = []

    def urlopen(request, timeout, **options):
        seen.append(request.full_url)
        return io.BytesIO(b'{"data": {}, "revision": "r"}')

    monkeypatch.setattr(api_client.urllib.request, "urlopen", urlopen)
    return seen


@pytest.mark.parametrize("credentials", CREDENTIALS)
@pytest.mark.parametrize("base", ["http://ledger:8765", "http://10.0.0.7:8765", "http://ledger.swarm.svc:8765"])
def test_a_credential_to_a_plain_http_host_off_loopback_is_refused(monkeypatch, base, credentials):
    seen = opened(monkeypatch)

    with pytest.raises(ledger_link.Cleartext, match="https"):
        api_client.ResourceClient(base, credentials).request(SLUG, "metadata")
    assert seen == []


@pytest.mark.parametrize("credentials", CREDENTIALS)
@pytest.mark.parametrize("base", ["http://127.0.0.1:8765", "http://localhost:8765", "https://ledger.swarm.svc:8765"])
def test_a_credential_to_loopback_or_https_is_sent(monkeypatch, base, credentials):
    seen = opened(monkeypatch)

    api_client.ResourceClient(base, credentials).request(SLUG, "metadata")
    assert seen == [f"{base}/api/v1/ledgers/{SLUG}/metadata"]


def test_a_plain_http_request_without_a_credential_is_sent(monkeypatch):
    seen = opened(monkeypatch)

    api_client.ResourceClient("http://ledger.test", {}).request(SLUG, "chat")
    assert seen == [f"http://ledger.test/api/v1/ledgers/{SLUG}/chat"]


def test_the_proof_controller_refuses_its_credential_over_plain_http_off_loopback(monkeypatch):
    environ = {"AGENTIHOOKS_CONTROLLER_CREDENTIAL": "controller", "AGENTIHOOKS_DEPLOYMENT": "distributed"}
    monkeypatch.setattr(proof_ledger.urllib.request, "urlopen", pytest.fail)

    with pytest.raises(ledger_link.Cleartext):
        proof_ledger.call("ledgers", {}, {**environ, "LEDGER_URL": "http://ledger:8765"})


def test_the_local_base_is_https_when_the_ledger_serves_tls(tmp_path):
    environ = {"LEDGER_DIR": str(tmp_path), "LEDGER_PORT": "8766"}

    assert ledger_link.base(environ) == "http://127.0.0.1:8766"
    assert ledger_link.base({**environ, "LEDGER_TLS_CERT": "c.pem", "LEDGER_TLS_KEY": "k.pem"}) == (
        "https://127.0.0.1:8766"
    )


def test_a_tls_ledger_admits_its_https_page_origin(tmp_path):
    environ = {
        "LEDGER_DIR": str(tmp_path),
        "LEDGER_PORT": "8766",
        "LEDGER_TLS_CERT": "c.pem",
        "LEDGER_TLS_KEY": "k.pem",
    }

    assert "https://127.0.0.1:8766" in ledger_link.allowed_origins(environ)
    assert "http://127.0.0.1:8766" not in ledger_link.allowed_origins(environ)


@pytest.mark.parametrize("half", [{"LEDGER_TLS_CERT": "c.pem"}, {"LEDGER_TLS_KEY": "k.pem"}])
def test_tls_needs_both_the_certificate_and_the_key(half):
    with pytest.raises(SystemExit, match="LEDGER_TLS_CERT and LEDGER_TLS_KEY"):
        ledger_link.server_context(half)


def test_no_tls_settings_serve_plain_http():
    assert ledger_link.server_context({}) is None


@pytest.fixture
def tls_ledger(cert):
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    handler = type(
        "TLSHandler",
        (ledger_server.Handler,),
        {"tls": ledger_link.server_context(dict(zip(("LEDGER_TLS_CERT", "LEDGER_TLS_KEY"), cert, strict=True)))},
    )
    httpd = Burst(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    page = legacy_page.render(new_ledger.build_doc({"title": "TLS", "phases": [{"title": "Proof"}]}), SLUG, port)
    core.paths(SLUG)[0].write_text(page)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    with (
        patch("hooks._redis.get_redis", return_value=None),
        patch.object(ledger_server, "ALLOWED_HOSTS", {f"localhost:{port}"}),
        patch.dict(os.environ, {"LEDGER_CA_PEM": Path(cert[0]).read_text()}),
    ):
        thread.start()
        try:
            yield {"base": f"https://localhost:{port}", "admin": core.read_token(page)}
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)


def test_a_credential_reaches_the_ledger_over_tls(tls_ledger):
    reply = api_client.ResourceClient(tls_ledger["base"], {"X-Ledger-Token": tls_ledger["admin"]}).request(
        SLUG, "metadata"
    )

    assert reply["data"]["title"] == "TLS"


def test_the_health_check_answers_over_tls(tls_ledger):
    assert ledger_link.serving(timeout=5, url=tls_ledger["base"]) == str(core.LEDGER_DIR)
