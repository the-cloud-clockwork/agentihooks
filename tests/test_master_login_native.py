import io
import subprocess
from http.client import HTTPConnection
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest

from scripts import master_login_native as native


def _request(url, method="POST", body="proof#state", origin="https://platform.claude.com"):
    parsed = urlsplit(url)
    connection = HTTPConnection(parsed.hostname, parsed.port, timeout=2)
    try:
        connection.request(method, parsed.path, body=body, headers={"Origin": origin, "Content-Type": "text/plain"})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        return result
    finally:
        connection.close()


def _process(monkeypatch, output):
    process = SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO(output))
    spawned = Mock(return_value=process)
    monkeypatch.setattr(native.subprocess, "Popen", spawned)
    monkeypatch.setattr(native.shutil, "which", lambda name: "/native/claude" if name == "claude" else None)
    return process, spawned


@pytest.mark.parametrize(
    "url",
    [
        "https://claude.ai/oauth/authorize?state=proof",
        "https://claude.com/oauth/authorize?state=proof",
        "https://claude.com/cai/oauth/authorize?state=proof",
    ],
)
def test_authorization_url_preserves_the_native_request(url):
    assert native.authorization_url(f"Open {url}\x1b[0m\n") == url


@pytest.mark.parametrize(
    "text",
    [
        "https://claude.com.evil.example/cai/oauth/authorize?state=proof",
        "http://claude.com/cai/oauth/authorize?state=proof",
        "https://example.com/oauth/authorize?state=proof",
        "native login failed",
    ],
)
def test_authorization_url_rejects_non_authorization_output(text):
    assert native.authorization_url(text) is None


def test_native_callback_goes_directly_to_stdin_once_without_logs(tmp_path, monkeypatch, capsys):
    from scripts import master_claude_login as login

    url = "https://claude.com/cai/oauth/authorize?state=proof"
    process, spawned = _process(monkeypatch, f"Open {url}\nprivate output\n")
    stop = Mock()
    env = {"AH_CC_TOKEN_worker": "proof", "ANTHROPIC_API_KEY": "proof"}
    target = tmp_path / "claude"
    with native.start(env, target, stop) as attempt:
        assert attempt.process is process
        assert attempt.output.read_text() == url
        parsed = urlsplit(attempt.callback)
        assert (parsed.scheme, parsed.hostname, parsed.path) == ("http", "127.0.0.1", "/complete")
        status, headers, body = _request(attempt.callback, "OPTIONS", "")
        assert status == 204 and body == b""
        assert headers["Access-Control-Allow-Origin"] == "https://platform.claude.com"
        assert headers["Access-Control-Allow-Private-Network"] == "true"
        assert headers["Access-Control-Allow-Methods"] == "POST, OPTIONS"
        assert _request(attempt.callback) == (204, headers, b"")
        assert process.stdin.getvalue() == "proof#state\n"
        assert _request(attempt.callback)[0] == 409
        assert process.stdin.getvalue() == "proof#state\n"
        assert attempt.output.read_text() == url
    stop.assert_called_once_with(process)
    argv = spawned.call_args.args[0]
    assert argv == ["/native/claude", *login.SETTINGS, *login.LOGIN]
    options = spawned.call_args.kwargs
    assert options["stdin"] == options["stdout"] == subprocess.PIPE
    assert options["stderr"] == subprocess.STDOUT and options["text"] is True
    assert options["env"] == {**login.child(env, target), "BROWSER": "true"}
    assert list(tmp_path.iterdir()) == []
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "path,origin,body,status",
    [
        ("/wrong", "https://platform.claude.com", "proof", 403),
        ("/complete", "https://evil.example", "proof", 403),
        ("/complete", "https://platform.claude.com", "", 400),
        ("/complete", "https://platform.claude.com", "one\ntwo", 400),
        ("/complete", "https://platform.claude.com", "x" * 4097, 413),
    ],
)
def test_native_callback_rejects_untrusted_or_invalid_input(tmp_path, monkeypatch, path, origin, body, status):
    process, _ = _process(monkeypatch, "no authorization URL\n")
    with native.start({}, tmp_path / "claude", Mock()) as attempt:
        assert attempt.output.read_text() == ""
        endpoint = attempt.callback.removesuffix("/complete") + path
        assert _request(endpoint, body=body, origin=origin)[0] == status
        assert process.stdin.getvalue() == ""


def test_native_callback_handles_a_closed_native_input_without_logging(tmp_path, monkeypatch, capsys):
    process, _ = _process(monkeypatch, "")
    with native.start({}, tmp_path / "claude", Mock()) as attempt:
        process.stdin.close()
        assert _request(attempt.callback)[0] == 410
    assert capsys.readouterr() == ("", "")


def test_native_resources_stop_when_the_browser_wait_raises(tmp_path, monkeypatch):
    process, _ = _process(monkeypatch, "")
    stop = Mock()
    with pytest.raises(RuntimeError, match="wait ended"):
        with native.start({}, tmp_path / "claude", stop):
            raise RuntimeError("wait ended")
    stop.assert_called_once_with(process)
