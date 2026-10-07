import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hooks.classifier import YesNo, fallbacks
from hooks.classifier.errors import BackendFailure
from hooks.classifier.result import DecisionRequest
from scripts import claude_quota_balancer
from scripts.claude_quota_balancer import Credential, RoutingError

REQUEST = DecisionRequest("typo", {"simple": YesNo("Small?", true="yes", false="no")})
RAW = {"answers": {"simple": {"noul": 1}}}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")
    picks = []

    def select(environ):
        picks.append(dict(environ))
        return SimpleNamespace(credential=Credential("AH_CC_TOKEN_other", environ["AH_CC_TOKEN_other"]))

    monkeypatch.setattr(fallbacks, "select_credential", select)
    return picks


def _never_run(*args, **kwargs):
    raise AssertionError("claude started")


@pytest.mark.parametrize(
    "native,route,token,expected",
    [
        (None, "routed", "parent-placeholder", "parent-placeholder"),
        ("explicit-placeholder", "routed", "parent-placeholder", "explicit-placeholder"),
        (None, "routed", None, "other-placeholder"),
        (None, None, "parent-placeholder", "other-placeholder"),
        ("", "routed", "parent-placeholder", "parent-placeholder"),
    ],
)
def test_claude_child_preserves_routed_parent_oauth(monkeypatch, native, route, token, expected):
    for name, value in (
        ("CLAUDE_CODE_OAUTH_TOKEN", native),
        ("AGENTIHOOKS_ROUTE_ACCOUNT", route),
        ("AH_CC_TOKEN_routed", token),
    ):
        if value is not None:
            monkeypatch.setenv(name, value)
    monkeypatch.setenv("AH_CC_TOKEN_other", "other-placeholder")
    monkeypatch.setenv("AH_CC_TOKEN_XXXX", "unselected-placeholder")
    seen = []

    def run(args, **kwargs):
        seen.append(kwargs["env"])
        return SimpleNamespace(returncode=0, stdout=json.dumps({"structured_output": RAW}))

    monkeypatch.setattr(fallbacks.subprocess, "run", run)
    assert fallbacks.ClaudeCliBackend().decide(REQUEST).source == "haiku"
    assert seen[0].get("CLAUDE_CODE_OAUTH_TOKEN") == expected
    assert seen[0]["AGENTIHOOKS_CLASSIFIER_CHILD"] == "1"
    assert seen[0].get("AGENTIHOOKS_ROUTE_ACCOUNT") == route


def test_unrouted_child_picks_among_every_account_token(monkeypatch, _env):
    monkeypatch.setenv("AH_CC_TOKEN_routed", "parent-placeholder")
    monkeypatch.setenv("AH_CC_TOKEN_other", "other-placeholder")
    seen = []

    def run(args, **kwargs):
        seen.append(kwargs["env"])
        return SimpleNamespace(returncode=0, stdout=json.dumps({"structured_output": RAW}))

    monkeypatch.setattr(fallbacks.subprocess, "run", run)
    fallbacks.ClaudeCliBackend().decide(REQUEST)
    assert {"AH_CC_TOKEN_routed", "AH_CC_TOKEN_other"} <= set(_env[0])
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in _env[0]
    assert seen[0]["CLAUDE_CODE_OAUTH_TOKEN"] == "other-placeholder"


def test_no_account_token_fails_before_claude_starts(monkeypatch):
    monkeypatch.setattr(fallbacks, "select_credential", claude_quota_balancer.select_credential)
    monkeypatch.setattr(fallbacks.subprocess, "run", _never_run)
    with pytest.raises(BackendFailure, match=r"^no Claude account: no non-empty AH_CC_TOKEN_\* variables found$"):
        fallbacks.ClaudeCliBackend().decide(REQUEST)


def test_unroutable_accounts_fail_before_claude_starts(monkeypatch):
    monkeypatch.setenv("AH_CC_TOKEN_other", "other-placeholder")

    def select(environ):
        raise RoutingError("no Claude account has verified routing capacity")

    monkeypatch.setattr(fallbacks, "select_credential", select)
    monkeypatch.setattr(fallbacks.subprocess, "run", _never_run)
    with pytest.raises(BackendFailure, match="^no Claude account: no Claude account has verified routing capacity$"):
        fallbacks.ClaudeCliBackend().decide(REQUEST)


def test_codex_keeps_native_auth_environment(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_ROUTE_ACCOUNT", "routed")
    monkeypatch.setenv("AH_CC_TOKEN_routed", "parent-placeholder")

    def run(args, **kwargs):
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in kwargs["env"]
        Path(args[args.index("--output-last-message") + 1]).write_text(json.dumps(RAW))
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(fallbacks.subprocess, "run", run)
    assert fallbacks.CodexCliBackend().decide(REQUEST).source == "luna"
