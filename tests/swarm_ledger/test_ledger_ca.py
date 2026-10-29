import io
import json
import ssl
import sys
import urllib.error
from pathlib import Path

import pytest

from scripts import init_reporter, openbao_kv
from scripts.swarm_ledger import ledger_ca, ledger_link
from tests.swarm_ledger import test_ledger_tls
from tests.swarm_ledger.test_ledger_authority import core

cert, tls_ledger = test_ledger_tls.cert, test_ledger_tls.tls_ledger

URL = "https://bao.example.test"
SECRET_URL = f"{URL}/v1/secret/data/k8s/agentihooks-swarm-ledger-ca"


def opener(answer, seen):
    def urlopen(request, timeout):
        seen.append((request.full_url, request.get_header("X-vault-token"), timeout))
        if isinstance(answer, Exception):
            raise answer
        return io.BytesIO(json.dumps(answer).encode())

    return urlopen


def bao(tmp_path, **extra):
    return {"OPENBAO_URL": URL, "OPENBAO_TOKEN": "bao", "AGENTIHOOKS_HOME": str(tmp_path), **extra}


def test_the_ca_file_sits_in_the_agentihooks_home(tmp_path):
    assert ledger_link.ca_file({"AGENTIHOOKS_HOME": str(tmp_path)}) == tmp_path / "ledger-ca.pem"
    assert ledger_link.ca_file({}) == Path.home() / ".agentihooks" / "ledger-ca.pem"
    assert ledger_link.ca_file({"LEDGER_CA_FILE": "~/ca.pem", "AGENTIHOOKS_HOME": "/x"}) == Path.home() / "ca.pem"


def test_install_writes_the_certificate_from_openbao(tmp_path, cert):
    pem, seen = Path(cert[0]).read_text(), []
    answer = {"data": {"data": {"ca.crt": pem, "tls.key": "never"}}}

    said = ledger_ca.install(bao(tmp_path), opener(answer, seen))

    assert said == f"ledger CA: {tmp_path / 'ledger-ca.pem'}"
    assert (tmp_path / "ledger-ca.pem").read_text() == pem
    assert seen == [(SECRET_URL, "bao", openbao_kv.TIMEOUT_S)]


def test_install_creates_the_agentihooks_home(tmp_path, cert):
    home = tmp_path / "fresh" / "home"
    answer = {"data": {"data": {"ca.crt": Path(cert[0]).read_text()}}}

    ledger_ca.install(bao(home), opener(answer, []))

    assert (home / "ledger-ca.pem").is_file()


@pytest.mark.parametrize("value", [None, "", "  \n", "not a certificate", "key", "pair"])
def test_install_keeps_the_file_when_openbao_holds_no_certificate(tmp_path, cert, value):
    pems = {"key": Path(cert[1]).read_text(), "pair": Path(cert[0]).read_text() + Path(cert[1]).read_text()}
    data = {} if value is None else {"ca.crt": pems.get(value, value)}
    (tmp_path / "ledger-ca.pem").write_text("kept")

    said = ledger_ca.install(bao(tmp_path), opener({"data": {"data": data}}, []))

    assert said == "ledger CA: no certificate in OpenBao k8s/agentihooks-swarm-ledger-ca"
    assert (tmp_path / "ledger-ca.pem").read_text() == "kept"


def test_install_keeps_the_file_when_the_secret_is_missing(tmp_path):
    missing = urllib.error.HTTPError(SECRET_URL, 404, "Not Found", {}, io.BytesIO(b"{}"))

    said = ledger_ca.install(bao(tmp_path), opener(missing, []))

    assert said == "ledger CA: no certificate in OpenBao k8s/agentihooks-swarm-ledger-ca"
    assert not (tmp_path / "ledger-ca.pem").exists()


@pytest.mark.parametrize("unset", ["OPENBAO_URL", "OPENBAO_TOKEN"])
def test_install_skips_without_openbao(tmp_path, unset):
    environ = bao(tmp_path)
    del environ[unset]
    seen = []

    said = ledger_ca.install(environ, opener({}, seen))

    assert said == "ledger CA: no OpenBao"
    assert seen == []
    assert not (tmp_path / "ledger-ca.pem").exists()


def test_install_reports_an_unreachable_openbao(tmp_path):
    said = ledger_ca.install(bao(tmp_path), opener(urllib.error.URLError("refused"), []))

    assert said == "ledger CA: OpenBao unavailable"
    assert not (tmp_path / "ledger-ca.pem").exists()


def spied(monkeypatch):
    made, real = [], ssl.create_default_context

    def create(*args, **kwargs):
        made.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(ledger_link.ssl, "create_default_context", create)
    return made


def subjects(context):
    return [dict(part[0] for part in found["subject"]).get("commonName") for found in context.get_ca_certs()]


def test_the_ca_file_is_trusted_beside_the_system_roots(monkeypatch, cert):
    made = spied(monkeypatch)

    context = ledger_link.transport({"LEDGER_CA_FILE": cert[0]})["context"]

    assert made == [((), {})]
    assert "localhost" in subjects(context)


def test_the_ca_pem_wins_over_the_ca_file(monkeypatch, cert):
    pem = Path(cert[0]).read_text()
    made = spied(monkeypatch)

    ledger_link.transport({"LEDGER_CA_PEM": pem, "LEDGER_CA_FILE": "/no/such/ca.pem"})

    assert made == [((), {"cadata": pem.strip()})]


def test_no_pem_and_no_ca_file_leave_the_default_transport(tmp_path):
    assert ledger_link.transport({"AGENTIHOOKS_HOME": str(tmp_path)}) == {}


def test_a_named_ca_file_that_is_missing_is_refused(tmp_path):
    missing = tmp_path / "missing.pem"

    with pytest.raises(SystemExit) as refused:
        ledger_link.transport({"LEDGER_CA_FILE": str(missing)})

    assert str(refused.value) == f"LEDGER_CA_FILE names no file: {missing}"


@pytest.mark.parametrize("named", [True, False])
def test_a_ca_file_that_holds_no_certificate_is_named(tmp_path, named):
    bad = tmp_path / "ledger-ca.pem"
    bad.write_text("not a certificate\n")
    environ = {"LEDGER_CA_FILE": str(bad)} if named else {"AGENTIHOOKS_HOME": str(tmp_path)}
    with pytest.raises(ssl.SSLError) as raw:
        ssl.create_default_context().load_verify_locations(cafile=str(bad))

    with pytest.raises(SystemExit) as refused:
        ledger_link.transport(environ)

    assert str(refused.value) == f"the ledger CA file {bad} holds no valid certificate: {raw.value.reason}"
    assert isinstance(refused.value.__cause__, ssl.SSLError)


def test_the_metrics_command_trusts_the_ledger_ca(monkeypatch, cert, capsys):
    from scripts.swarm_ledger import request_metrics

    seen = []

    def urlopen(request, timeout, **options):
        seen.append((request.full_url, timeout, options, request.get_header("Cf-access-client-id")))
        return io.BytesIO(b"{}")

    monkeypatch.delenv("LEDGER_CA_PEM", raising=False)
    monkeypatch.setenv("LEDGER_CA_FILE", cert[0])
    monkeypatch.setenv("SWARM_ACCESS_CLIENT_ID", "client")
    monkeypatch.setenv("SWARM_ACCESS_CLIENT_SECRET", "secret")
    monkeypatch.setattr(ledger_link.urllib.request, "urlopen", urlopen)

    request_metrics.main(["--url", "https://ledger.example:8765"])

    [(url, timeout, options, client)] = seen
    assert (url, timeout, client) == ("https://ledger.example:8765/api/v1/metrics?hours=24.0", 10, "client")
    assert isinstance(options["context"], ssl.SSLContext)
    assert capsys.readouterr().out == "{}\n"


def test_the_metrics_command_defaults_to_the_ledger_scheme(monkeypatch, cert):
    from scripts.swarm_ledger import request_metrics

    seen = []
    monkeypatch.setenv("LEDGER_TLS_CERT", cert[0])
    monkeypatch.setenv("LEDGER_TLS_KEY", cert[1])
    monkeypatch.setattr(ledger_link, "address", lambda environ: ("localhost", 9999))
    monkeypatch.setattr(request_metrics, "urlopen", lambda url, timeout: seen.append(url) or io.StringIO("{}"))

    request_metrics.main([])

    assert seen == ["https://localhost:9999/api/v1/metrics?hours=24.0"]


def test_the_metrics_command_defaults_to_the_hosted_ledger(monkeypatch):
    from scripts.swarm_ledger import request_metrics

    seen = []
    monkeypatch.setenv("SWARM_URL", "https://ledger.example/")
    monkeypatch.setattr(request_metrics, "urlopen", lambda url, timeout: seen.append(url) or io.StringIO("{}"))

    request_metrics.main([])

    assert seen == ["https://ledger.example/api/v1/metrics?hours=24.0"]


def test_a_client_verifies_the_hosted_ledger_through_the_ca_file(tls_ledger, cert, monkeypatch, tmp_path):
    monkeypatch.delenv("LEDGER_CA_PEM")
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    monkeypatch.delenv("LEDGER_CA_FILE", raising=False)

    assert ledger_link.serving(timeout=5, url=tls_ledger["base"]) is None

    (tmp_path / "ledger-ca.pem").write_text(Path(cert[0]).read_text())

    assert ledger_link.serving(timeout=5, url=tls_ledger["base"]) == str(core.LEDGER_DIR)


def test_init_installs_the_ledger_ca(monkeypatch, capsys):
    from scripts import herdr_setup, install

    monkeypatch.delenv("AGENTIHOOKS_TARGET", raising=False)
    monkeypatch.setattr(install, "_load_state", lambda: {})
    monkeypatch.setattr(init_reporter, "report_target", lambda *args, **kwargs: None)
    monkeypatch.setattr(herdr_setup, "init_step", lambda *args: None)
    monkeypatch.setattr(ledger_ca, "install", lambda: "ledger CA: installed")
    monkeypatch.setattr(sys, "argv", ["agentihooks", "init", "--profile", "default", "--target", "claude", "--verbose"])

    install.main()

    assert capsys.readouterr().out.splitlines()[-1] == "ledger CA: installed"
