import io
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import master_login_native as native


def _process(monkeypatch, output):
    process = SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO(output))
    spawned = Mock(return_value=process)
    monkeypatch.setattr(native.subprocess, "Popen", spawned)
    monkeypatch.setattr(
        native.shutil, "which", lambda name: "/native/claude" if name == "claude" else "/windows/powershell.exe"
    )
    return process, spawned


def _clipboard(monkeypatch, value, returncode=0):
    capture = Mock(return_value=SimpleNamespace(stdout=value, returncode=returncode))
    monkeypatch.setattr(native.subprocess, "run", capture)
    return capture


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
    capture = _clipboard(monkeypatch, "code#proof\r\n")
    stop = Mock()
    env = {"AH_CC_TOKEN_worker": "proof", "ANTHROPIC_API_KEY": "proof"}
    target = tmp_path / "claude"
    with native.start(env, target, stop) as attempt:
        attempt.reader.join(timeout=1)
        assert attempt.process is process
        assert attempt.output.read_text() == url
        capture.assert_not_called()
        assert attempt.complete() is True
        assert process.stdin.getvalue() == "code#proof\n"
        assert attempt.complete() is True
        capture.assert_called_once()
        assert process.stdin.getvalue() == "code#proof\n"
        assert attempt.output.read_text() == url
    stop.assert_called_once_with(process)
    assert spawned.call_args.args[0] == ["/native/claude", *login.SETTINGS, *login.LOGIN]
    options = spawned.call_args.kwargs
    assert options["stdin"] == options["stdout"] == subprocess.PIPE
    assert options["stderr"] == subprocess.STDOUT and options["text"] is True
    assert options["env"] == {**login.child(env, target), "BROWSER": "true"}
    args, options = capture.call_args
    assert args[0][:4] == ["/windows/powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]
    assert "Get-Clipboard -Raw" in args[0][4] and "Set-Clipboard -Value ''" in args[0][4]
    assert options == {"capture_output": True, "text": True, "timeout": 15}
    assert process.stdin.closed and process.stdout.closed and not attempt.reader.is_alive()
    assert list(tmp_path.iterdir()) == []
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "value",
    [
        "",
        "unrelated",
        "code#other",
        "one\ntwo#proof",
        "x" * 4097 + "#proof",
        "#proof",
        "code#proof extra",
    ],
)
def test_native_callback_rejects_untrusted_or_invalid_input(tmp_path, monkeypatch, value):
    process, _ = _process(monkeypatch, "https://claude.com/cai/oauth/authorize?state=proof\n")
    capture = _clipboard(monkeypatch, value)
    with native.start({}, tmp_path / "claude", Mock()) as attempt:
        attempt.reader.join(timeout=1)
        assert attempt.complete() is False
        assert attempt.complete() is False
        capture.assert_called_once()
        assert process.stdin.getvalue() == ""


def test_native_callback_handles_a_closed_native_input_without_logging(tmp_path, monkeypatch, capsys):
    process, _ = _process(monkeypatch, "https://claude.com/cai/oauth/authorize?state=proof\n")
    _clipboard(monkeypatch, "code#proof")
    with native.start({}, tmp_path / "claude", Mock()) as attempt:
        attempt.reader.join(timeout=1)
        process.stdin.close()
        assert attempt.complete() is False
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("failure", [OSError("unavailable"), subprocess.TimeoutExpired("clipboard", 15)])
def test_native_callback_capture_failure_is_private(tmp_path, monkeypatch, failure, capsys):
    process, _ = _process(monkeypatch, "https://claude.com/cai/oauth/authorize?state=proof\n")
    capture = Mock(side_effect=failure)
    monkeypatch.setattr(native.subprocess, "run", capture)
    with native.start({}, tmp_path / "claude", Mock()) as attempt:
        attempt.reader.join(timeout=1)
        assert attempt.complete() is False
        assert process.stdin.getvalue() == ""
    assert capsys.readouterr() == ("", "")


def test_native_callback_refuses_missing_state(tmp_path, monkeypatch):
    process, _ = _process(monkeypatch, "https://claude.com/cai/oauth/authorize?client_id=proof\n")
    capture = _clipboard(monkeypatch, "unrelated")
    with native.start({}, tmp_path / "claude", Mock()) as attempt:
        attempt.reader.join(timeout=1)
        assert attempt.complete() is False
        capture.assert_not_called()
        assert process.stdin.getvalue() == ""


def test_native_callback_refuses_failed_clipboard_command(tmp_path, monkeypatch):
    process, _ = _process(monkeypatch, "https://claude.com/cai/oauth/authorize?state=proof\n")
    _clipboard(monkeypatch, "code#proof", returncode=1)
    with native.start({}, tmp_path / "claude", Mock()) as attempt:
        attempt.reader.join(timeout=1)
        assert attempt.complete() is False
        assert process.stdin.getvalue() == ""


def test_native_resources_stop_when_the_browser_wait_raises(tmp_path, monkeypatch):
    process, _ = _process(monkeypatch, "")
    stop = Mock()
    with pytest.raises(RuntimeError, match="wait ended"):
        with native.start({}, tmp_path / "claude", stop):
            raise RuntimeError("wait ended")
    stop.assert_called_once_with(process)
    assert process.stdin.closed and process.stdout.closed
