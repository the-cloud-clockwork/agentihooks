import io
import json
import socket
import ssl
import struct
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
    return _raw(port, b"POST /hive/refresh HTTP/1.0\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))


UNKNOWN = {"error": "the ledger credential belongs to no hive member"}


def test_the_endpoint_answers_200_with_the_grant_and_403_for_an_unknown_credential(admin, hive):
    member = auth.mint(admin, "laptop", PUBLIC)

    status, grant = _post(hive, json.dumps({"ledger_credential": member["ledger_credential"]}).encode())

    assert status == 200
    assert grant["id"] == member["id"]
    assert _post(hive, json.dumps({"ledger_credential": "unknown"}).encode()) == (403, UNKNOWN)


def test_a_join_is_not_found(hive):
    assert _raw(hive, b"POST /hive/join HTTP/1.0\r\nContent-Length: 2\r\n\r\n{}") == (404, {"error": "not found"})


def test_a_request_without_content_length_is_refused(hive):
    assert _raw(hive, b"POST /hive/refresh HTTP/1.0\r\n\r\n") == (400, {"error": server.BAD_REFRESH_BODY})


@pytest.mark.parametrize(("size", "status"), [(server.MAX_BODY, 403), (server.MAX_BODY + 1, 400)])
def test_the_body_limit_is_inclusive(hive, size, status):
    body = b'{"ledger_credential": "' + b"x" * (size - 25) + b'"}'
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


def stalled_tls_client(admin, cert, monkeypatch):
    monkeypatch.setattr(server, "REQUEST_TIMEOUT_S", 0.5)
    httpd = server.make_server(admin, PUBLIC, "localhost", 0, cert)
    port = _serve(httpd)
    try:
        with socket.create_connection(("localhost", port), timeout=5) as stalled:
            started = time.monotonic()
            return stalled.recv(1), time.monotonic() - started
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_stalled_tls_client_is_dropped(admin, cert, monkeypatch):
    assert stalled_tls_client(admin, cert, monkeypatch)[0] == b""


@pytest.mark.wall_clock
def test_a_stalled_tls_client_is_dropped_after_the_request_timeout(admin, cert, monkeypatch):
    assert stalled_tls_client(admin, cert, monkeypatch)[1] < 3


def test_an_https_refresh_completes_the_tls_handshake(admin, cert, tmp_path, monkeypatch, capsys):
    httpd = server.make_server(admin, PUBLIC, "localhost", 0, cert)
    port = _serve(httpd)
    trusting = ssl.create_default_context(cafile=cert[0])
    real = urllib.request.urlopen
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda request, timeout: real(request, timeout=timeout, context=trusting)
    )
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    monkeypatch.setenv("AGENTIHOOKS_HIVE_URL", f"https://localhost:{port}")
    monkeypatch.setenv("AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL", auth.mint(admin, "laptop", PUBLIC)["ledger_credential"])
    try:
        assert cli.main(["refresh"]) == 0
    finally:
        httpd.shutdown()
        httpd.server_close()

    assert capsys.readouterr().out.startswith("refreshed the Redis credential of hive member ")


def _refresh_over_tls(admin, cert, tmp_path, monkeypatch):
    httpd = server.make_server(admin, PUBLIC, "localhost", 0, cert)
    port = _serve(httpd)
    for name in ("LEDGER_CA_PEM", "LEDGER_CA_FILE"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    monkeypatch.setenv("AGENTIHOOKS_HIVE_URL", f"https://localhost:{port}")
    monkeypatch.setenv("AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL", auth.mint(admin, "laptop", PUBLIC)["ledger_credential"])
    return httpd


def _refresh(httpd):
    try:
        return cli.main(["refresh"])
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_refresh_trusts_the_ledger_ca_pem(admin, cert, tmp_path, monkeypatch, capsys):
    httpd = _refresh_over_tls(admin, cert, tmp_path, monkeypatch)
    monkeypatch.setenv("LEDGER_CA_PEM", Path(cert[0]).read_text())

    assert _refresh(httpd) == 0
    assert capsys.readouterr().out.startswith("refreshed the Redis credential of hive member ")


def test_a_refresh_trusts_the_saved_ledger_ca_file(admin, cert, tmp_path, monkeypatch, capsys):
    httpd = _refresh_over_tls(admin, cert, tmp_path, monkeypatch)
    (tmp_path / "ledger-ca.pem").write_text(Path(cert[0]).read_text())

    assert _refresh(httpd) == 0
    assert capsys.readouterr().out.startswith("refreshed the Redis credential of hive member ")


def test_a_refresh_without_the_ledger_ca_refuses_the_unverified_hive(admin, cert, tmp_path, monkeypatch, capsys):
    httpd = _refresh_over_tls(admin, cert, tmp_path, monkeypatch)

    assert _refresh(httpd) == 1
    assert "CERTIFICATE_VERIFY_FAILED" in capsys.readouterr().err


def _head_and_body(port, request):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
        conn.sendall(request)
        reply = b""
        while chunk := conn.recv(65536):
            reply += chunk
    head, _, body = reply.partition(b"\r\n\r\n")
    return head.decode().split("\r\n"), body


@pytest.mark.parametrize(
    ("request_line", "body", "expected"),
    [
        (b"POST /hive/refresh HTTP/1.0", b'{"ledger_credential": "unknown"}', UNKNOWN),
        (b"POST /elsewhere HTTP/1.0", b"{}", {"error": "not found"}),
        (b"POST /hive/refresh HTTP/1.0", b"[]", {"error": server.BAD_REFRESH_BODY}),
    ],
)
def test_every_answer_carries_its_length(hive, request_line, body, expected):
    head, answer = _head_and_body(hive, request_line + b"\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))

    assert f"Content-Length: {len(answer)}" in head
    assert json.loads(answer) == expected


@pytest.fixture
def tls_hive(admin, cert):
    httpd = server.make_server(admin, PUBLIC, "localhost", 0, cert)
    port = _serve(httpd)
    yield port
    httpd.shutdown()
    httpd.server_close()


def test_curl_reads_a_full_https_refresh_with_no_stream_error(admin, cert, tls_hive, capsys):
    member = auth.mint(admin, "laptop", PUBLIC)

    result = subprocess.run(
        [
            "curl",
            "--silent",
            "--show-error",
            "--include",
            "--cacert",
            cert[0],
            "--data",
            json.dumps({"ledger_credential": member["ledger_credential"]}),
            f"https://localhost:{tls_hive}{server.REFRESH_PATH}",
        ],
        capture_output=True,
        timeout=30,
    )

    head, _, body = result.stdout.partition(b"\r\n\r\n")
    assert (result.returncode, result.stderr) == (0, b"")
    assert b"Content-Length: %d" % len(body) in head.split(b"\r\n")
    assert json.loads(body)["id"] == member["id"]
    assert capsys.readouterr().err == ""


def test_the_server_ends_tls_with_a_close_notify(cert, tls_hive):
    strict = ssl.create_default_context(cafile=cert[0])
    with socket.create_connection(("localhost", tls_hive), timeout=5) as raw:
        with strict.wrap_socket(raw, server_hostname="localhost", suppress_ragged_eofs=False) as conn:
            conn.sendall(b"POST /elsewhere HTTP/1.0\r\nContent-Length: 2\r\n\r\n{}")
            reply = b""
            while chunk := conn.recv(65536):
                reply += chunk

    assert reply.startswith(b"HTTP/1.0 404 ")
    assert reply.endswith(b'{"error": "not found"}')


def _process_one(httpd, client):
    request, address = httpd.get_request()
    client()
    httpd.process_request_thread(request, address)


def _half_hello(conn):
    conn.sendall(b"\x16\x03\x01\x02\x00\x01\x00")
    conn.close()


@pytest.mark.parametrize("drop", [socket.socket.close, _half_hello])
def test_a_connection_dropped_before_or_during_the_tls_handshake_logs_nothing(admin, cert, monkeypatch, capsys, drop):
    httpd = server.make_server(admin, PUBLIC, "localhost", 0, cert)
    errors = []
    monkeypatch.setattr(
        httpd, "handle_error", lambda *args: errors.append(sys.exc_info()[1]) or type(httpd).handle_error(httpd, *args)
    )
    probe = socket.create_connection(("localhost", httpd.server_address[1]), timeout=5)
    try:
        _process_one(httpd, lambda: drop(probe))
    finally:
        probe.close()
        httpd.server_close()

    assert [type(e) for e in errors] == [server.Dropped]
    assert isinstance(errors[0].__cause__, ssl.SSLError)
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("error", [RuntimeError, ConnectionError])
def test_an_unexpected_handler_error_is_still_logged(admin, monkeypatch, capsys, error):
    def broken(*_):
        raise error("refresh broke")

    monkeypatch.setattr(auth, "refresh", broken)
    httpd = server.make_server(admin, PUBLIC, "127.0.0.1", 0)
    client = socket.create_connection(("127.0.0.1", httpd.server_address[1]), timeout=5)
    address = client.getsockname()
    try:
        _process_one(
            httpd,
            lambda: client.sendall(
                b'POST /hive/refresh HTTP/1.0\r\nContent-Length: 25\r\n\r\n{"ledger_credential": ""}'
            ),
        )
    finally:
        client.close()
        httpd.server_close()

    err = capsys.readouterr().err
    assert f"{error.__name__}: refresh broke" in err
    assert f"Exception occurred during processing of request from {address}\n" in err


def test_refresh_names_the_status_of_an_error_without_a_json_body(tmp_path, monkeypatch, capsys):
    def urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 502, "Bad Gateway", {}, io.BytesIO(b"<html>"))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    monkeypatch.setenv("AGENTIHOOKS_HIVE_URL", "https://hive.example")
    monkeypatch.setenv("AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL", "led")

    assert cli.main(["refresh"]) == 1

    assert capsys.readouterr().err == "hive refresh refused: HTTP 502\n"


def test_refresh_names_an_unreachable_hive(tmp_path, monkeypatch, capsys):
    def urlopen(request, timeout):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    monkeypatch.setenv("AGENTIHOOKS_HIVE_URL", "https://hive.example")
    monkeypatch.setenv("AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL", "led")

    assert cli.main(["refresh"]) == 1

    assert capsys.readouterr().err == (
        "hive refresh refused: the hive at https://hive.example is unreachable (<urlopen error refused>)\n"
    )


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
        "Save an SSH machine in herdr and deliver its member credential to it",
        "Delete a member's ledger credential and Redis user",
        "Write a new controller service credential to a private file, retiring the previous one",
        "Run the hive refresh endpoint",
        "Set name, ui, ephemeral, excluded, prefer, max-agents as key=value",
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

    def make_server(*args, **kwargs):
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
    assert printed == [((f"hive endpoint on {scheme}://h:4321/hive/refresh",), {"flush": True})]


@pytest.mark.parametrize("flags", [["--tls-cert", "/c.pem"], ["--tls-key", "/k.pem"]])
def test_serve_refuses_a_lone_tls_flag_before_starting(served, flags):
    calls, printed = served

    assert cli.main(["serve", *flags]) == 1

    assert calls == []
    assert printed == [(("hive serve refused: --tls-cert and --tls-key go together",), {"file": sys.stderr})]


def test_a_handler_error_is_still_logged_when_the_client_resets_before_the_tls_close(admin, cert, monkeypatch, capsys):
    raised = threading.Event()

    def broken(*_):
        raised.set()
        raise RuntimeError("refresh broke")

    monkeypatch.setattr(auth, "refresh", broken)
    httpd = server.make_server(admin, PUBLIC, "localhost", 0, cert)
    trusting = ssl.create_default_context(cafile=cert[0])

    def client():
        raw = socket.create_connection(("localhost", httpd.server_address[1]), timeout=5)
        conn = trusting.wrap_socket(raw, server_hostname="localhost")
        conn.sendall(b'POST /hive/refresh HTTP/1.0\r\nContent-Length: 25\r\n\r\n{"ledger_credential": ""}')
        raised.wait(5)
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        conn.close()

    resetting = threading.Thread(target=client)
    resetting.start()
    try:
        request, address = httpd.get_request()
        httpd.process_request_thread(request, address)
    finally:
        resetting.join()
        httpd.server_close()

    assert "RuntimeError: refresh broke" in capsys.readouterr().err
