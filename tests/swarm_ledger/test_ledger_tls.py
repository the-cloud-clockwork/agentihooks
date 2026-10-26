import io
import os
import socket
import ssl
import subprocess
import threading
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.swarm_ledger import ledger, ledger_health, ledger_link, ledger_server, proof_ledger, watch_ledger
from scripts.swarm_ledger.api import client as api_client
from tests.swarm_ledger import legacy_page
from tests.swarm_ledger.test_ledger_authority import SLUG, core, new_ledger

CREDENTIALS = [
    {"X-Controller-Credential": "controller"},
    {"X-Hive-Credential": "hive", "X-Ledger-Agent": "engineer@323133-0256"},
    {"X-Ledger-Token": "token"},
    {"X-Proof-Credential": "proof"},
    {"x-ledger-token": "lower case"},
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
@pytest.mark.parametrize(
    "base", ["http://127.0.0.1:8765", "http://localhost:8765", "http://[::1]:8765", "https://ledger.swarm.svc:8765"]
)
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


def test_proof_controls_refuse_their_credentials_over_plain_http_off_loopback(monkeypatch):
    monkeypatch.setattr(proof_ledger.urllib.request, "urlopen", pytest.fail)
    environ = {"AGENTIHOOKS_DEPLOYMENT": "distributed", "LEDGER_URL": "http://ledger:8765"}

    with pytest.raises(ledger_link.Cleartext):
        proof_ledger._control_call(SLUG, "controls", {}, {"X-Proof-Credential": "proof"}, environ)


def test_the_event_stream_refuses_its_credential_over_plain_http_off_loopback(monkeypatch):
    monkeypatch.setattr(watch_ledger.ledger_link, "base", lambda: "http://ledger:8765")
    monkeypatch.setattr(watch_ledger.urllib.request, "urlopen", pytest.fail)

    with pytest.raises(watch_ledger.ledger_link.Cleartext):
        next(watch_ledger.stream(SLUG, None, {"X-Ledger-Token": "token"}))


def test_an_upload_refuses_its_credential_over_plain_http_off_loopback(monkeypatch, tmp_path):
    (tmp_path / "shot.png").write_bytes(b"png")
    monkeypatch.setattr(ledger, "base", lambda: "http://ledger:8765")
    monkeypatch.setattr(ledger, "credentials", lambda slug: {"X-Ledger-Token": "token"})
    monkeypatch.setattr(ledger.urllib.request, "urlopen", pytest.fail)

    with pytest.raises(SystemExit, match="https"):
        ledger.upload(SLUG, "engineer@323133-0256", str(tmp_path / "shot.png"), "media", {})


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
def tls_ledger(cert, monkeypatch):
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    handler = type(
        "TLSHandler",
        (ledger_server.Handler,),
        {"tls": ledger_link.server_context(dict(zip(("LEDGER_TLS_CERT", "LEDGER_TLS_KEY"), cert, strict=True)))},
    )
    httpd = ledger_server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    page = legacy_page.render(new_ledger.build_doc({"title": "TLS", "phases": [{"title": "Proof"}]}), SLUG, port)
    legacy_page.paths(SLUG, core)[0].write_text(page)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    with (
        patch("hooks._redis.get_redis", return_value=None),
        patch.object(ledger_server, "ALLOWED_HOSTS", {f"localhost:{port}"}),
        patch.dict(os.environ),
    ):
        monkeypatch.setenv("LEDGER_CA_PEM", Path(cert[0]).read_text())
        thread.start()
        try:
            yield {"base": f"https://localhost:{port}", "port": port, "admin": core.read_token(page)}
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)


def test_a_credential_reaches_the_ledger_over_tls(tls_ledger):
    reply = api_client.ResourceClient(tls_ledger["base"], {"X-Ledger-Token": tls_ledger["admin"]}).request(
        SLUG, "metadata"
    )

    assert reply["data"]["title"] == "TLS"


def test_a_plain_http_client_on_the_tls_port_is_dropped_quietly(tls_ledger, capsys):
    with socket.create_connection(("127.0.0.1", tls_ledger["port"]), timeout=5) as plain:
        plain.sendall(b"GET /healthz HTTP/1.0\r\n\r\n")
        try:
            reply = plain.recv(1024)
        except ConnectionResetError:
            reply = b""
        assert not reply.startswith(b"HTTP/")

    assert ledger_link.serving(timeout=5, url=tls_ledger["base"]) == str(core.LEDGER_DIR)
    assert "Traceback" not in capsys.readouterr().err


def test_the_health_check_answers_over_tls(tls_ledger):
    assert ledger_link.serving(timeout=5, url=tls_ledger["base"]) == str(core.LEDGER_DIR)


@pytest.mark.parametrize("path", ["agent-token", "ops"])
def test_a_refusal_with_an_unread_body_answers_with_its_status_over_tls(tls_ledger, path):
    request = urllib.request.Request(
        f"{tls_ledger['base']}/api/v1/ledgers/{SLUG}/{path}",
        b'{"ops": []}',
        {"Content-Type": "application/json", "X-Ledger-Agent": "engineer@323133-0256"},
        method="POST",
    )
    context = ssl.create_default_context(cadata=os.environ["LEDGER_CA_PEM"])

    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(request, timeout=5, context=context)

    assert refused.value.code == 403
    assert b"forbidden" in refused.value.read()


def trusted(monkeypatch, cert):
    seen = []

    def urlopen(request, timeout, **options):
        seen.append((request.full_url.split("/api/")[0], timeout, isinstance(options.get("context"), ssl.SSLContext)))
        return io.BytesIO(b'{"data": {"credential": "issued"}, "revision": "r"}')

    monkeypatch.setenv("LEDGER_CA_PEM", Path(cert[0]).read_text())
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return seen


def test_every_credential_sender_trusts_the_ledger_ca_with_its_own_timeout(monkeypatch, cert, tmp_path):
    seen = trusted(monkeypatch, cert)
    environ = {**os.environ, "AGENTIHOOKS_DEPLOYMENT": "distributed", "LEDGER_URL": "https://ledger:8765"}
    (tmp_path / "shot.png").write_bytes(b"png")
    monkeypatch.setattr(ledger, "base", lambda: "https://ledger:8765")
    monkeypatch.setattr(ledger, "credentials", lambda slug: {"X-Ledger-Token": "token"})
    monkeypatch.setattr(watch_ledger.ledger_link, "base", lambda environ=None: "https://ledger:8765")

    monkeypatch.delenv("LEDGER_CA_PEM")
    proof_ledger.call("ledgers", {}, {**environ, "AGENTIHOOKS_CONTROLLER_CREDENTIAL": "controller"})
    proof_ledger._control_call(SLUG, "controls", {}, {"X-Proof-Credential": "proof"}, environ)
    monkeypatch.setenv("LEDGER_CA_PEM", environ["LEDGER_CA_PEM"])
    api_client.ResourceClient("https://ledger:8765", {"X-Ledger-Token": "token"}, timeout=3).request(SLUG, "chat")
    ledger.upload(SLUG, "engineer@323133-0256", str(tmp_path / "shot.png"), "media", {})
    list(watch_ledger.stream(SLUG, None, {"X-Ledger-Token": "token"}))

    assert seen == [
        ("https://ledger:8765", proof_ledger.TIMEOUT, True),
        ("https://ledger:8765", proof_ledger.TIMEOUT, True),
        ("https://ledger:8765", 3, True),
        ("https://ledger:8765", ledger.REQUEST_TIMEOUT, True),
        ("https://ledger:8765", watch_ledger.STREAM_TIMEOUT_S, True),
    ]


def test_the_health_probe_trusts_the_ledger_ca(monkeypatch, cert):
    seen = []

    def urlopen(request, timeout, **options):
        seen.append((timeout, isinstance(options.get("context"), ssl.SSLContext)))
        return io.BytesIO(b"{}")

    monkeypatch.setenv("LEDGER_CA_PEM", Path(cert[0]).read_text())

    assert ledger_health.probe("https://127.0.0.1:8765", urlopen) == ""
    assert seen == [(ledger_health.WRITE_TIMEOUT_S, True)]


def test_only_a_failed_handshake_is_kept_out_of_the_error_log(capsys):
    server = ledger_server.ThreadingHTTPServer(("127.0.0.1", 0), ledger_server.Handler)
    try:
        for error in (ledger_server.Handshake(), ValueError("broken handler")):
            try:
                raise error
            except Exception:
                server.handle_error(None, ("192.0.2.7", 4242))
    finally:
        server.server_close()

    logged = capsys.readouterr().err
    assert "('192.0.2.7', 4242)" in logged
    assert "broken handler" in logged
    assert "Handshake" not in logged


def test_a_client_that_never_starts_the_handshake_is_dropped(tls_ledger, monkeypatch):
    monkeypatch.setattr(ledger_server, "TLS_HANDSHAKE_S", 0.3)
    with socket.create_connection(("127.0.0.1", tls_ledger["port"]), timeout=5) as stalled:
        try:
            reply = stalled.recv(1)
        except ConnectionResetError:
            reply = b""

    assert reply == b""
