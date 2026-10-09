import io
import json
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.hive import auth, cli, registry, server

pytestmark = pytest.mark.xdist_group("fakeredis")

PUBLIC = "redis://hive.example:6380/2"


@pytest.fixture
def admin():
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


def _serve(httpd):
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd.server_address[1]


@pytest.fixture
def hive(admin, monkeypatch):
    monkeypatch.setattr(server, "REQUEST_TIMEOUT_S", 0.5)
    httpd = server.make_server(admin, PUBLIC, "127.0.0.1", 0)
    port = _serve(httpd)
    yield port
    httpd.shutdown()
    httpd.server_close()


def _raw(port, request):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
        conn.sendall(request)
        reply = b""
        while chunk := conn.recv(65536):
            reply += chunk
    head, _, body = reply.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(body)


def _post(port, body):
    return _raw(port, b"POST /hive/join HTTP/1.0\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))


def test_the_endpoint_answers_200_with_the_grant_and_403_for_a_used_code(admin, hive):
    code = auth.invite(admin, "laptop")

    status, grant = _post(hive, json.dumps({"code": code}).encode())

    assert status == 200
    assert grant["name"] == "laptop"
    assert _post(hive, json.dumps({"code": code}).encode()) == (
        403,
        {"error": "invite code is invalid, expired or already used"},
    )


def test_a_request_without_content_length_is_refused(hive):
    assert _raw(hive, b"POST /hive/join HTTP/1.0\r\n\r\n") == (400, {"error": server.BAD_BODY})


@pytest.mark.parametrize(("size", "status"), [(server.MAX_BODY, 403), (server.MAX_BODY + 1, 400)])
def test_the_body_limit_is_inclusive(hive, size, status):
    body = b'{"code": "' + b"x" * (size - 12) + b'"}'
    assert len(body) == size

    assert _post(hive, body)[0] == status


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


def test_a_stalled_tls_client_is_dropped_after_the_request_timeout(admin, cert, monkeypatch):
    monkeypatch.setattr(server, "REQUEST_TIMEOUT_S", 0.5)
    httpd = server.make_server(admin, PUBLIC, "localhost", 0, cert)
    port = _serve(httpd)
    try:
        with socket.create_connection(("localhost", port), timeout=5) as stalled:
            started = time.monotonic()
            assert stalled.recv(1) == b""
            assert time.monotonic() - started < 3
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_an_https_join_completes_the_tls_handshake(admin, cert, tmp_path, monkeypatch, capsys):
    httpd = server.make_server(admin, PUBLIC, "localhost", 0, cert)
    port = _serve(httpd)
    trusting = ssl.create_default_context(cafile=cert[0])
    real = urllib.request.urlopen
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda request, timeout: real(request, timeout=timeout, context=trusting)
    )
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    try:
        assert cli.main(["join", f"https://localhost:{port}", auth.invite(admin, "laptop")]) == 0
    finally:
        httpd.shutdown()
        httpd.server_close()

    assert capsys.readouterr().out.startswith("joined the hive as ")


def test_join_posts_the_code_to_the_join_path_with_the_request_timeout(tmp_path, monkeypatch, capsys):
    sent = {}

    def urlopen(request, timeout):
        sent.update(url=request.full_url, method=request.get_method(), data=request.data, timeout=timeout)
        return io.BytesIO(b'{"id": "ab12", "ledger_credential": "led", "redis_url": "redis://u:p@h:1/0"}')

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))

    assert cli.main(["join", "https://hive.example/baseX/", "the-code"]) == 0

    assert sent == {
        "url": "https://hive.example/baseX/hive/join",
        "method": "POST",
        "data": b'{"code": "the-code"}',
        "timeout": server.REQUEST_TIMEOUT_S,
    }
    assert capsys.readouterr().out == f"joined the hive as ab12; credentials are in {tmp_path / 'hive.env'}\n"


def test_join_names_the_status_of_an_error_without_a_json_body(tmp_path, monkeypatch, capsys):
    def urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 502, "Bad Gateway", {}, io.BytesIO(b"<html>"))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))

    assert cli.main(["join", "https://hive.example", "code"]) == 1

    assert capsys.readouterr().err == "hive join refused: HTTP 502\n"


def test_is_loopback_refuses_a_missing_host():
    assert server.is_loopback(None) is False


def test_the_default_home_is_dot_agentihooks(monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_HOME", raising=False)

    assert registry.home() == Path.home() / ".agentihooks"


def test_help_names_every_command_and_option(monkeypatch, capsys):
    monkeypatch.setenv("COLUMNS", "300")

    top = cli._parser().format_help()
    with pytest.raises(SystemExit):
        cli.main(["serve", "--help"])
    serve = capsys.readouterr().out

    assert top.startswith("usage: agentihooks hive ")
    assert "\n\nHive credentials for remote swarm hosts\n" in top
    for text in (
        "Print a one-time join code, valid fifteen minutes",
        "Exchange a join code for credentials in hive.env",
        "Delete a member's ledger credential and Redis user",
        "Write a new controller service credential to a private file, retiring the previous one",
        "Run the join endpoint",
        "Set name, ui, ephemeral, roles, prefer, max-agents as key=value",
        "Print a hive's record as JSON",
        "One line per hive with its liveness",
    ):
        assert f" {text}\n" in top
    for text in (
        "Redis URL members connect to; defaults to this host's",
        "PEM certificate; required off loopback",
        "PEM private key for --tls-cert",
    ):
        assert f" {text}\n" in serve


def test_a_command_is_required(capsys):
    with pytest.raises(SystemExit) as refused:
        cli.main([])

    assert refused.value.code == 2
    assert "the following arguments are required: command" in capsys.readouterr().err


def test_serve_defaults_and_port_type():
    assert vars(cli._parser().parse_args(["serve"])) == {
        "command": "serve",
        "host": "127.0.0.1",
        "port": 8770,
        "redis_url": None,
        "tls_cert": None,
        "tls_key": None,
    }
    assert cli._parser().parse_args(["serve", "--port", "9"]).port == 9


@pytest.fixture
def served(admin, monkeypatch):
    calls, printed = [], []

    def make_server(*args):
        calls.append(args)
        return SimpleNamespace(server_address=("127.0.0.1", 4321), serve_forever=lambda: None)

    monkeypatch.setattr(server, "make_server", make_server)
    monkeypatch.setattr(cli, "redis_client", lambda: admin)
    monkeypatch.setattr(cli, "print", lambda *args, **kwargs: printed.append((args, kwargs)), raising=False)
    monkeypatch.setenv("AGENTIHOOKS_SWARM_REDIS_URL", "redis://own:6379/0")
    return calls, printed


@pytest.mark.parametrize(
    ("flags", "redis_url", "tls", "scheme"),
    [
        ([], "redis://own:6379/0", None, "http"),
        (["--redis-url", "redis://public:6379/0"], "redis://public:6379/0", None, "http"),
        (["--tls-cert", "/c.pem", "--tls-key", "/k.pem"], "redis://own:6379/0", ("/c.pem", "/k.pem"), "https"),
    ],
)
def test_serve_passes_its_settings_and_announces_the_endpoint(admin, served, flags, redis_url, tls, scheme):
    calls, printed = served

    assert cli.main(["serve", "--host", "h", "--port", "5", *flags]) == 0

    assert calls == [(admin, redis_url, "h", 5, tls)]
    assert printed == [((f"hive join endpoint on {scheme}://h:4321/hive/join",), {"flush": True})]


@pytest.mark.parametrize("flags", [["--tls-cert", "/c.pem"], ["--tls-key", "/k.pem"]])
def test_serve_refuses_a_lone_tls_flag_before_starting(served, flags):
    calls, printed = served

    assert cli.main(["serve", *flags]) == 1

    assert calls == []
    assert printed == [(("hive serve refused: --tls-cert and --tls-key go together",), {"file": sys.stderr})]
