import io
import json
import ssl
import threading
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.hive import auth, cli, server
from scripts.swarm_ledger import ledger, ledger_ca
from tests.swarm_ledger import test_ledger_tls
from tests.swarm_ledger.test_ledger_authority import SLUG, core

cert, tls_ledger = test_ledger_tls.cert, test_ledger_tls.tls_ledger

PUBLIC = "redis://hive.example:6380/2"
MEMBER_REDIS = "redis://" + "member@h:1/0"


@pytest.fixture
def admin():
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


@pytest.fixture
def pem(cert):
    return Path(cert[0]).read_text()


def _serving(httpd):
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return httpd.server_address[1]


def _post(port, path, body):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode())
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


@pytest.mark.parametrize("trusted", [True, False])
def test_the_refresh_answer_carries_the_ledger_ca_the_service_trusts(admin, pem, trusted):
    grant = auth.mint(admin, "laptop", PUBLIC)
    httpd = server.make_server(admin, PUBLIC, "127.0.0.1", 0, ledger_ca=pem if trusted else None)
    port = _serving(httpd)
    try:
        refreshed = _post(port, server.REFRESH_PATH, {"ledger_credential": grant["ledger_credential"]})
    finally:
        httpd.shutdown()
        httpd.server_close()

    assert refreshed.get("ledger_ca") == (pem if trusted else None)
    assert sorted(refreshed) == sorted(["id", "redis_url", *(["ledger_ca"] if trusted else [])])


def _granted(monkeypatch, tmp_path, **extra):
    body = {"id": "ab12", "ledger_credential": "led", "redis_url": MEMBER_REDIS, **extra}
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout: io.BytesIO(json.dumps(body).encode()))
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))


def test_refresh_without_a_valid_ledger_ca_writes_none(tmp_path, monkeypatch):
    for extra in ({}, {"ledger_ca": ""}, {"ledger_ca": "not a certificate"}, {"ledger_ca": 7}):
        _granted(monkeypatch, tmp_path, **extra)
        environ = {"AGENTIHOOKS_HIVE_URL": "https://hive.example", "AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL": "led"}
        environ["AGENTIHOOKS_HOME"] = str(tmp_path)

        assert cli.refresh_env(environ) == "ab12"

        assert not (tmp_path / "ledger-ca.pem").exists()


def test_refresh_rewrites_the_ledger_ca_in_the_hive_home(tmp_path, monkeypatch, pem):
    _granted(monkeypatch, tmp_path, ledger_ca=pem)
    environ = {"AGENTIHOOKS_HIVE_URL": "https://hive.example", "AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL": "led"}
    environ["AGENTIHOOKS_HOME"] = str(tmp_path)

    assert cli.refresh_env(environ) == "ab12"

    assert (tmp_path / "ledger-ca.pem").read_text() == pem


def test_refresh_writes_the_ledger_ca_to_the_file_its_own_environment_names(tmp_path, monkeypatch, pem):
    _granted(monkeypatch, tmp_path, ledger_ca=pem)
    environ = {"AGENTIHOOKS_HIVE_URL": "https://hive.example", "AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL": "led"}
    environ["LEDGER_CA_FILE"] = str(tmp_path / "named.pem")

    assert cli.refresh_env(environ) == "ab12"

    assert (tmp_path / "named.pem").read_text() == pem
    assert not (tmp_path / "ledger-ca.pem").exists()


def test_place_creates_the_given_home_whatever_the_environment_names(tmp_path, pem):
    home = tmp_path / "fresh" / "home"

    assert ledger_ca.place(home, pem, {"AGENTIHOOKS_HOME": str(tmp_path / "elsewhere")}) == home / "ledger-ca.pem"

    assert (home / "ledger-ca.pem").read_text() == pem
    assert sorted(path.name for path in home.iterdir()) == ["ledger-ca.pem"]
    assert not (tmp_path / "elsewhere").exists()


def test_served_reads_the_ca_pem_then_the_ca_file(tmp_path, pem):
    (tmp_path / "ledger-ca.pem").write_text(pem)

    assert ledger_ca.served({"LEDGER_CA_PEM": pem, "AGENTIHOOKS_HOME": "/no/such/home"}) == pem
    assert ledger_ca.served({"AGENTIHOOKS_HOME": str(tmp_path)}) == pem
    assert ledger_ca.served({"AGENTIHOOKS_HOME": str(tmp_path / "empty")}) is None
    assert ledger_ca.served({"LEDGER_CA_PEM": "not a certificate", "AGENTIHOOKS_HOME": str(tmp_path)}) is None


def test_serve_hands_the_join_service_the_ledger_ca_it_trusts(admin, monkeypatch, pem):
    calls = []

    def make_server(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(server_address=("127.0.0.1", 4321), serve_forever=lambda: None)

    monkeypatch.setattr(server, "make_server", make_server)
    monkeypatch.setattr(cli, "redis_client", lambda: admin)
    monkeypatch.setenv("LEDGER_CA_PEM", pem)

    assert cli.main(["serve", "--redis-url", "redis://public:6379/0"]) == 0

    assert calls == [((admin, "redis://public:6379/0", "127.0.0.1", 8770, None), {"ledger_ca": pem})]


def test_a_master_from_a_freshly_added_hive_passes_its_ledger_join(admin, cert, pem, tls_ledger, tmp_path, monkeypatch):
    home = tmp_path / "hive-home"
    for name in ("LEDGER_CA_PEM", "LEDGER_CA_FILE", "SSL_CERT_FILE", "SWARM_URL", "LEDGER_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(home))
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "local")
    monkeypatch.setenv("LEDGER_AUTOSTART", "0")
    monkeypatch.setattr(ledger, "base", lambda slug="": tls_ledger["base"])
    monkeypatch.setattr(ledger, "credentials", lambda slug, service=False: {"X-Ledger-Token": tls_ledger["admin"]})
    master = SimpleNamespace(slug=SLUG, name="master@hive-proof", role="member")

    with pytest.raises(urllib.error.URLError) as untrusted:
        ledger.request(SLUG, [ledger.op("join", master, role="member")])
    assert isinstance(untrusted.value.reason, ssl.SSLCertVerificationError)

    httpd = server.make_server(admin, PUBLIC, "localhost", 0, cert, ledger_ca=pem)
    port = _serving(httpd)
    trusting = ssl.create_default_context(cafile=cert[0])
    real = urllib.request.urlopen
    try:
        with monkeypatch.context() as joining:
            joining.setattr(
                urllib.request, "urlopen", lambda request, timeout: real(request, timeout=timeout, context=trusting)
            )
            auth.write_env(home, f"https://localhost:{port}", auth.mint(admin, "workstation", PUBLIC))
            assert cli.main(["refresh"]) == 0
    finally:
        httpd.shutdown()
        httpd.server_close()

    ledger.send(master, "join", role="member")

    assert "master@hive-proof" in core.sync(SLUG)[0]["_meta"]["members"]
