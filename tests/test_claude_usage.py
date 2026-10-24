import http.client
import io
import itertools
import json
import subprocess
import threading
import urllib.error
from email.message import Message

import pytest

from scripts import claude_quota_balancer as balancer
from scripts import claude_usage
from tests.test_claude_quota_balancer import _stream

PREFIX = "anthropic-ratelimit-unified-"


def _headers(five=0.20, week=0.30, status="allowed", **extra):
    found = Message()
    values = {
        "status": status,
        "5h-utilization": five,
        "5h-reset": 4600,
        "7d-utilization": week,
        "7d-reset": 91000,
        **extra,
    }
    for name, value in values.items():
        if value is not None:
            found[f"{PREFIX}{name}"] = str(value)
    return found


class Response(io.BytesIO):
    def __init__(self, status, headers, body):
        super().__init__(body if isinstance(body, bytes) else json.dumps(body).encode())
        self.status, self.headers = status, headers


BODY = {"model": "claude-haiku-4-5-20251001", "usage": {"input_tokens": 10, "cache_creation_input_tokens": 990}}


def _opener(status=200, headers=None, body=None, error=None, seen=None):
    def opener(request, timeout=None):
        if seen is not None:
            seen.append((request, timeout))
        if error is not None:
            raise error
        found = _headers() if headers is None else headers
        if status != 200:
            raise urllib.error.HTTPError(request.full_url, status, "err", found, io.BytesIO(b'{"type": "error"}'))
        return Response(status, found, BODY if body is None else body)

    return opener


def test_the_headers_map_onto_the_windows_and_the_status():
    found = claude_usage.result("a", _headers(0.09, 0.69), BODY, 120)
    assert (found.five_hour, found.seven_day) == (
        balancer.QuotaWindow(9.0, 4600),
        balancer.QuotaWindow(69.0, 91000),
    )
    assert (found.provider_status, found.state, found.margin) == ("allowed", "REDUCE", 31.0)
    assert (found.model, found.context_used, found.latency_ms, found.error) == (BODY["model"], 1000, 120, "")


def test_a_rejected_status_reads_as_blocked():
    found = claude_usage.result("a", _headers(status="rejected"), {}, 5)
    assert (found.state, found.margin, found.model, found.context_used) == ("BLOCKED", 0.0, "?", None)


@pytest.mark.parametrize("five", [None, "soon", "inf", "nan"])
def test_a_missing_or_unreadable_window_reads_as_unavailable(five):
    found = claude_usage.result("a", _headers(five=five), BODY, 5)
    assert (found.state, found.five_hour.used, found.error) == ("UNKNOWN", None, "rate-limit windows unavailable")


def test_a_missing_status_reads_as_unknown_status():
    headers = _headers()
    del headers[f"{PREFIX}status"]
    assert claude_usage.result("a", headers, BODY, 5).provider_status == "unknown"


@pytest.fixture
def clock(monkeypatch):
    ticks = itertools.count(10.0, 2.5)
    monkeypatch.setattr(claude_usage.time, "monotonic", lambda: next(ticks))


@pytest.mark.parametrize("window", ["5h", "7d"])
def test_a_rejected_window_is_blocked_whatever_the_aggregate_says(window):
    headers = _headers(**{f"{window}-status": "rejected"})
    assert claude_usage.result("a", headers, {}, 5).state == "BLOCKED"
    del headers[f"{PREFIX}status"]
    assert claude_usage.result("a", headers, {}, 5).provider_status == "rejected"


@pytest.mark.parametrize("value", ["n/a", float("inf")])
def test_a_bad_usage_field_leaves_the_context_unknown(value):
    found = claude_usage.result("a", _headers(), {"usage": {"input_tokens": value}}, 5)
    assert (found.state, found.context_used) == ("NORMAL", None)


def test_a_broken_response_is_an_error_for_that_account_only(monkeypatch):
    def opener(request, timeout=None):
        if request.get_header("Authorization") == "Bearer tok-B":
            raise http.client.IncompleteRead(b"")
        return Response(200, _headers(), BODY)

    monkeypatch.setattr(claude_usage.urllib.request, "urlopen", opener)
    monkeypatch.setattr(balancer.subprocess, "run", _no_cli)
    credentials = [balancer.Credential(f"AH_CC_TOKEN_{name}", f"tok-{name}") for name in ("A", "B", "C")]
    found = balancer.probe_credentials(credentials, "haiku", 60, {})
    assert [(item.account, item.state, item.error) for item in found] == [
        ("A", "NORMAL", ""),
        ("B", "ERROR", "bad response from the API"),
        ("C", "NORMAL", ""),
    ]


def test_a_token_that_is_not_a_valid_header_never_reaches_the_error():
    error = ValueError("Invalid header value b'Bearer abc\\n'")
    found = claude_usage.probe("a", "abc\n", "haiku", _opener(error=error))
    assert (found.account, found.state, found.error) == ("a", "ERROR", "the token is not a valid header value")
    assert "abc" not in found.error


class BrokenBody(io.BytesIO):
    def __init__(self, error):
        super().__init__(b"")
        self.error = error

    def read(self, *args):
        raise self.error


def _failing_body(error):
    def opener(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 500, "err", Message(), BrokenBody(error))

    return opener


@pytest.mark.parametrize(
    "error, expected",
    [
        (http.client.IncompleteRead(b""), "bad response from the API"),
        (TimeoutError("slow"), "probe timed out after 20s"),
    ],
)
def test_an_error_body_that_breaks_is_an_error_for_that_account(error, expected):
    found = claude_usage.probe("a", "tok", "haiku", _failing_body(error))
    assert (found.account, found.state, found.error) == ("a", "ERROR", expected)


def test_an_error_body_cut_by_the_network_is_unreachable():
    with pytest.raises(claude_usage.Unreachable):
        claude_usage.probe("a", "tok", "haiku", _failing_body(ConnectionResetError("reset")))


def test_the_caller_timeout_holds_under_the_ceiling(monkeypatch):
    seen = []
    monkeypatch.setattr(claude_usage.urllib.request, "urlopen", _opener(seen=seen))
    credential = balancer.Credential("AH_CC_TOKEN_A", "tok-a")
    balancer.probe_credential(credential, "haiku", 5, {})
    balancer.probe_credential(credential, "haiku", 60, {})
    assert [timeout for _, timeout in seen] == [5, 20]
    found = claude_usage.probe("a", "tok", "haiku", _opener(error=TimeoutError("slow")), timeout=5)
    assert found.error == "probe timed out after 5s"


def test_the_probe_sends_one_messages_request_with_the_oauth_headers(clock):
    seen = []
    found = claude_usage.probe("a", "tok-1", "haiku", _opener(seen=seen))
    assert (found.account, found.state, found.five_hour.used) == ("a", "NORMAL", 20.0)
    assert (found.model, found.context_used, found.latency_ms) == (BODY["model"], 1000, 2500)
    [(request, timeout)] = seen
    assert (request.full_url, request.get_method(), timeout) == ("https://api.anthropic.com/v1/messages", "POST", 20)
    assert request.get_header("Authorization") == "Bearer tok-1"
    assert request.get_header("Anthropic-beta") == "oauth-2025-04-20"
    assert request.get_header("Anthropic-version") == "2023-06-01"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data) == {
        "model": "claude-haiku-4-5",
        "max_tokens": 1,
        "system": "You are Claude Code, Anthropic's official CLI for Claude.",
        "messages": [{"role": "user", "content": "OK"}],
    }


def test_an_unknown_model_name_is_sent_as_given():
    seen = []
    claude_usage.probe("a", "tok", "claude-sonnet-x", _opener(seen=seen))
    assert json.loads(seen[0][0].data)["model"] == "claude-sonnet-x"


def test_a_429_with_rate_limit_headers_reads_like_a_200():
    found = claude_usage.probe("a", "tok", "haiku", _opener(429, _headers(1.0, 0.5, status="rejected")))
    assert (found.state, found.five_hour.used, found.error) == ("BLOCKED", 100.0, "")


@pytest.mark.parametrize("status, headers", [(401, Message()), (403, Message()), (429, Message()), (500, _headers())])
def test_any_other_answer_is_an_error_naming_the_status(status, headers, clock):
    found = claude_usage.probe("a", "tok", "haiku", _opener(status, headers))
    assert (found.account, found.state, found.error, found.latency_ms) == ("a", "ERROR", f"HTTP {status}", 2500)


@pytest.mark.parametrize("error", [TimeoutError("slow"), urllib.error.URLError(TimeoutError("slow"))])
def test_a_timeout_is_an_error_like_today(error):
    found = claude_usage.probe("a", "tok", "haiku", _opener(error=error))
    assert (found.account, found.state, found.error) == ("a", "ERROR", "probe timed out after 20s")


@pytest.mark.parametrize("error", [urllib.error.URLError("down"), ConnectionResetError("reset")])
def test_a_network_failure_is_unreachable(error):
    with pytest.raises(claude_usage.Unreachable):
        claude_usage.probe("a", "tok", "haiku", _opener(error=error))


def test_a_body_that_is_not_json_still_reads_the_headers():
    found = claude_usage.probe("a", "tok", "haiku", lambda request, timeout=None: Response(200, _headers(), b"<html>"))
    assert (found.state, found.model) == ("NORMAL", "?")


def _no_cli(*args, **kwargs):
    pytest.fail("the Claude CLI probe ran")


def test_probe_credential_uses_the_http_probe_and_never_starts_the_cli(monkeypatch):
    seen = []
    monkeypatch.setattr(claude_usage.urllib.request, "urlopen", _opener(seen=seen))
    monkeypatch.setattr(balancer.subprocess, "run", _no_cli)
    credential = balancer.Credential("AH_CC_TOKEN_A", "tok-a")
    found = balancer.probe_credential(credential, "haiku", 60, {"AH_CC_TOKEN_A": "tok-a"})
    assert (found.account, found.state, found.five_hour.used) == ("A", "NORMAL", 20.0)
    assert seen[0][0].get_header("Authorization") == "Bearer tok-a"
    assert json.loads(seen[0][0].data)["model"] == "claude-haiku-4-5"


def test_a_network_failure_falls_back_to_the_cli(monkeypatch):
    monkeypatch.setattr(claude_usage.urllib.request, "urlopen", _opener(error=urllib.error.URLError("down")))
    ran = []

    def run(command, **kwargs):
        ran.append(command[0])
        return subprocess.CompletedProcess(command, 0, _stream(0.20, 0.30), "")

    monkeypatch.setattr(balancer.subprocess, "run", run)
    found = balancer.probe_credential(balancer.Credential("AH_CC_TOKEN_A", "tok-a"), "haiku", 60, {})
    assert (ran, found.state) == (["claude"], "NORMAL")


def test_the_fable_probe_keeps_the_cli(monkeypatch):
    monkeypatch.setattr(claude_usage, "probe", lambda *a, **k: pytest.fail("http fable probe"))
    monkeypatch.setattr(
        balancer.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, _stream(0.20, 0.30, 0.40), ""),
    )
    credential = balancer.Credential("AH_CC_TOKEN_A", "tok-a")
    found = balancer.probe_credential(credential, "fable", 60, {}, include_fable=True)
    assert found.fable.used == 40.0


def test_accounts_are_probed_in_parallel_over_http(monkeypatch):
    barrier = threading.Barrier(3)

    def opener(request, timeout=None):
        barrier.wait(timeout=5)
        return Response(200, _headers(), BODY)

    monkeypatch.setattr(claude_usage.urllib.request, "urlopen", opener)
    monkeypatch.setattr(balancer.subprocess, "run", _no_cli)
    credentials = [balancer.Credential(f"AH_CC_TOKEN_{name}", f"tok-{name}") for name in ("A", "B", "C")]
    results = balancer.probe_credentials(credentials, "haiku", 60, {})
    assert [(found.account, found.state) for found in results] == [("A", "NORMAL"), ("B", "NORMAL"), ("C", "NORMAL")]


def test_the_balance_table_is_the_same_for_the_same_windows():
    http = claude_usage.result("A", _headers(0.20, 0.30), BODY, 100)
    cli = balancer.parse_probe("A", _stream(0.20, 0.30), 100)
    assert balancer.render_table([http], now=1000) == balancer.render_table([cli], now=1000)
