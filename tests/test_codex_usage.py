import io
import json
import subprocess
import urllib.error
from dataclasses import replace
from pathlib import Path

import pytest

from hooks import config
from scripts import agents_quota, codex_router, codex_usage
from scripts.claude_quota_balancer import QuotaWindow
from scripts.codex_quota import CodexQuota
from tests.codex_fakes import NOW, access_token

WEEK = {"used_percent": 12.0, "limit_window_seconds": 604800, "reset_after_seconds": 9, "reset_at": NOW + 100_000}
FIVE = {"used_percent": 40.0, "limit_window_seconds": 18000, "reset_at": NOW + 600}
PRO = {"plan_type": "pro", "user_id": "u-1", "rate_limit": {"allowed": True, "primary_window": WEEK}}


def _opener(body=None, error=None, seen=None):
    def opener(request, timeout=None):
        if seen is not None:
            seen.append((request, timeout))
        if error is not None:
            raise error
        return io.BytesIO(body if isinstance(body, bytes) else json.dumps(body).encode())

    return opener


def _http(code, body):
    return urllib.error.HTTPError(codex_usage.USAGE_URL, code, "err", {}, io.BytesIO(body))


@pytest.mark.parametrize(
    "limits, expected",
    [
        (
            {"primary_window": WEEK, "secondary_window": None},
            CodexQuota(NOW, "pro", seven_day=QuotaWindow(12.0, NOW + 100_000)),
        ),
        (
            {"primary_window": FIVE, "secondary_window": WEEK},
            CodexQuota(NOW, "pro", QuotaWindow(40.0, NOW + 600), QuotaWindow(12.0, NOW + 100_000)),
        ),
        ({"primary_window": {"used_percent": 3.0}}, CodexQuota(NOW, "pro")),
        (
            {"primary_window": {"limit_window_seconds": 604800}, "limit_reached": True},
            CodexQuota(NOW, "pro", seven_day=QuotaWindow(None, None), reached="limit_reached"),
        ),
        ({"limit_reached": True, "rate_limit_reached_type": "workspace"}, CodexQuota(NOW, "pro", reached="workspace")),
    ],
)
def test_windows_map_by_their_length_and_a_reached_limit_is_named(limits, expected):
    assert codex_usage.parse({"plan_type": "pro", "rate_limit": limits}, NOW) == expected


def test_a_body_without_rate_limits_or_a_plan_reads_as_nothing_or_unknown_plan():
    assert codex_usage.parse({"plan_type": "pro"}, NOW) is None
    assert codex_usage.parse({"rate_limit": {}}, NOW) == CodexQuota(NOW, "?")


def test_a_read_sends_the_bearer_the_workspace_and_the_codex_user_agent():
    seen = []
    assert codex_usage.read("tok-1", "ws-1", NOW, _opener(PRO, seen=seen)) == codex_usage.parse(PRO, NOW)
    [(request, timeout)] = seen
    assert (request.full_url, request.get_method(), timeout) == (codex_usage.USAGE_URL, "GET", 20)
    assert request.get_header("Authorization") == "Bearer tok-1"
    assert request.get_header("Chatgpt-account-id") == "ws-1"
    assert request.get_header("User-agent") == "codex_cli_rs"


@pytest.mark.parametrize(
    "body",
    [b'{"error": {"code": "token_expired", "message": "expired"}}', b'{"code": "token_expired"}'],
)
def test_an_expired_token_reads_as_expired(body):
    assert codex_usage.read("tok", "ws", NOW, _opener(error=_http(401, body))) == CodexQuota(NOW, "?", expired=True)


@pytest.mark.parametrize(
    "error, body",
    [
        (_http(401, b'{"error": {"code": "invalid"}}'), None),
        (_http(401, b"not json"), None),
        (_http(401, b"[]"), None),
        (_http(403, b'{"code": "token_expired"}'), None),
        (urllib.error.URLError("down"), None),
        (None, b"not json"),
        (None, b"[1]"),
        (None, json.dumps({"rate_limit": {"primary_window": {**WEEK, "used_percent": "x"}}}).encode()),
        (None, json.dumps({"rate_limit": {"primary_window": {**WEEK, "reset_at": "soon"}}}).encode()),
        (None, json.dumps({"rate_limit": {"primary_window": {**WEEK, "used_percent": [1]}}}).encode()),
        (None, json.dumps({"rate_limit": {"primary_window": {**WEEK, "reset_at": float("inf")}}}).encode()),
    ],
)
def test_any_other_failure_reads_as_nothing(error, body):
    assert codex_usage.read("tok", "ws", NOW, _opener(body, error)) is None


def test_the_default_opener_is_urlopen(monkeypatch):
    monkeypatch.setattr(codex_usage.urllib.request, "urlopen", _opener(PRO))
    assert codex_usage.read("tok", "ws", NOW) == codex_usage.parse(PRO, NOW)


def test_readings_are_cached_by_account_without_any_identity():
    first = replace(codex_usage.parse(PRO, NOW), reached="workspace")
    expired = CodexQuota(NOW + 5, "?", expired=True)
    assert codex_usage.cached() == {}
    codex_usage.store("alpha", first)
    codex_usage.store("beta", expired)
    assert codex_usage.cached() == {"alpha": first, "beta": expired}
    path = codex_usage._cache_path()
    assert path == Path(config.AGENTIHOOKS_HOME) / "codex-usage.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert "u-1" not in path.read_text() and "user_id" not in path.read_text()


@pytest.mark.parametrize("content", ["not json", "[]", '{"alpha": {"observed_at": 1}}'])
def test_an_unreadable_cache_reads_as_empty(content):
    path = codex_usage._cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    assert codex_usage.cached() == {}


def test_the_cache_folder_is_created_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "AGENTIHOOKS_HOME", str(tmp_path / "fresh" / "home"))
    reading = codex_usage.parse(PRO, NOW)
    codex_usage.store("alpha", reading)
    assert codex_usage.cached() == {"alpha": reading}


def test_a_cache_that_cannot_be_written_is_skipped(monkeypatch, tmp_path):
    monkeypatch.setattr(codex_usage, "_cache_path", lambda: tmp_path / "file" / "codex-usage.json")
    (tmp_path / "file").write_text("x")
    codex_usage.store("alpha", codex_usage.parse(PRO, NOW))


def _exec(ran):
    def run(argv, **kwargs):
        ran.append(1)
        return subprocess.CompletedProcess(argv, 0, "", "")

    return run


def _no_exec(*args, **kwargs):
    pytest.fail("the paid exec probe ran")


def test_a_chatgpt_token_account_reads_the_usage_endpoint_and_never_runs_exec(monkeypatch):
    seen = []
    monkeypatch.setattr(codex_usage.urllib.request, "urlopen", _opener(PRO, seen=seen))
    monkeypatch.setattr(codex_router.time, "time", lambda: NOW)
    token = access_token(5)
    account = codex_router.CodexAccount("alpha", "AH_CX_TOKEN_alpha")
    found = codex_router.probe(account, {"AH_CX_TOKEN_alpha": token, "HOME": "/h"}, _no_exec)
    assert found == codex_usage.parse(PRO, NOW)
    assert codex_usage.cached() == {"alpha": found}
    assert seen[0][0].get_header("Authorization") == f"Bearer {token}"
    assert seen[0][0].get_header("Chatgpt-account-id") == "ws-1"


def test_the_default_login_reads_its_file_token(monkeypatch, tmp_path):
    monkeypatch.setattr(codex_usage.urllib.request, "urlopen", _opener(PRO))
    monkeypatch.setattr(codex_router.time, "time", lambda: NOW)
    (tmp_path / "auth.json").write_text(json.dumps({"tokens": {"access_token": access_token(5)}}))
    found = codex_router.probe(codex_router.CodexAccount("default"), {"CODEX_HOME": str(tmp_path)}, _no_exec)
    assert found == codex_usage.parse(PRO, NOW)


def test_a_failed_usage_read_falls_back_to_exec_and_caches_nothing(monkeypatch):
    monkeypatch.setattr(codex_usage.urllib.request, "urlopen", _opener(error=urllib.error.URLError("down")))
    account = codex_router.CodexAccount("alpha", "AH_CX_TOKEN_alpha")
    ran = []
    assert codex_router.probe(account, {"AH_CX_TOKEN_alpha": access_token(5)}, _exec(ran)) is None
    assert ran == [1]
    assert codex_usage.cached() == {}


def test_an_expired_read_is_kept_and_never_runs_exec(monkeypatch):
    error = _http(401, b'{"code": "token_expired"}')
    monkeypatch.setattr(codex_usage.urllib.request, "urlopen", _opener(error=error))
    monkeypatch.setattr(codex_router.time, "time", lambda: NOW)
    account = codex_router.CodexAccount("alpha", "AH_CX_TOKEN_alpha")
    expired = CodexQuota(NOW, "?", expired=True)
    assert codex_router.probe(account, {"AH_CX_TOKEN_alpha": access_token(5)}, _no_exec) == expired
    assert codex_usage.cached() == {"alpha": expired}


def test_a_limit_reached_before_the_week_reset_stops_counting_after_it(monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_CODEX_READING_MAX_AGE_H", raising=False)
    blocked = CodexQuota(NOW - 3600, "pro", seven_day=QuotaWindow(100.0, NOW + 60), reached="workspace")
    assert (codex_router.account_cap(blocked, NOW), blocked.state_at(NOW)) == (0, "BLOCKED")
    after = NOW + 60
    assert (codex_router.account_cap(blocked, after), blocked.state_at(after)) == (6, "NORMAL")


@pytest.mark.parametrize(
    "auth",
    [None, "not json", {"tokens": None}, {"tokens": {"access_token": "opaque"}}, {"tokens": {"refresh_token": "r"}}],
)
def test_a_default_login_without_a_chatgpt_file_token_falls_back_to_exec(auth, monkeypatch, tmp_path):
    if auth is not None:
        (tmp_path / "auth.json").write_text(auth if isinstance(auth, str) else json.dumps(auth))
    ran = []
    monkeypatch.setattr(codex_usage, "read", lambda *a, **k: pytest.fail("usage read without a token"))
    codex_router.probe(codex_router.CodexAccount("default"), {"CODEX_HOME": str(tmp_path)}, _exec(ran))
    assert ran == [1]


def test_a_personal_access_token_and_an_api_account_never_read_usage(monkeypatch):
    monkeypatch.setattr(codex_usage, "read", lambda *a, **k: pytest.fail("usage read"))
    ran = []
    run = _exec(ran)
    codex_router.probe(codex_router.CodexAccount("alpha", "AH_CX_TOKEN_alpha"), {"AH_CX_TOKEN_alpha": "at-abc"}, run)
    assert (
        codex_router.probe(codex_router.CodexAccount("bad", "AH_CX_TOKEN_bad"), {"AH_CX_TOKEN_bad": "sk-x"}, run)
        is None
    )
    assert ran == [1]


def test_quotas_take_the_newest_of_the_session_log_and_the_usage_cache(monkeypatch):
    log = CodexQuota(NOW - 100, "pro", seven_day=QuotaWindow(50.0))
    monkeypatch.setattr(codex_router.codex_quota, "latest_codex_quota", lambda environ, keep: log)
    monkeypatch.setattr(codex_router, "_registry", lambda: {})
    newer = CodexQuota(NOW - 10, "pro", seven_day=QuotaWindow(20.0))
    older = CodexQuota(NOW - 900, "pro", seven_day=QuotaWindow(10.0))
    codex_usage.store("alpha", newer)
    codex_usage.store("beta", older)
    pool = [codex_router.CodexAccount(name, f"AH_CX_TOKEN_{name}") for name in ("alpha", "beta", "gamma")]
    assert codex_router.quotas(pool, {}) == {"alpha": newer, "beta": log, "gamma": log}


def test_an_expired_account_has_no_seats_and_shows_expired_with_the_login_never_stale(monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_CODEX_READING_MAX_AGE_H", raising=False)
    expired = CodexQuota(NOW, "?", expired=True)
    later = NOW + 7 * 3600
    assert codex_router.account_cap(expired, later) == 0
    assert expired.state == "EXPIRED"
    accounts = [codex_router.CodexAccount("alpha", "AH_CX_TOKEN_alpha"), codex_router.CodexAccount("default")]
    rows = agents_quota.codex_rows(accounts, {"alpha": expired, "default": expired}, {}, later)
    assert [(row.state, row.cap, row.source) for row in rows] == [
        ("EXPIRED", 0, "token expired: run agentihooks codex login alpha"),
        ("EXPIRED", 0, "token expired: run codex login"),
    ]
