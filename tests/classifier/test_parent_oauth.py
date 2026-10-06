import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hooks.classifier import YesNo, fallbacks
from hooks.classifier.result import DecisionRequest

REQUEST = DecisionRequest("typo", {"simple": YesNo("Small?", true="yes", false="no")})
RAW = {"answers": {"simple": {"noul": 1}}}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "AGENTIHOOKS_ROUTE_ACCOUNT", "AH_CC_TOKEN_routed", "AH_CC_TOKEN_other"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    "native,route,token,expected",
    [
        (None, "routed", "parent-placeholder", "parent-placeholder"),
        ("explicit-placeholder", "routed", "parent-placeholder", "explicit-placeholder"),
        (None, "routed", None, None),
        (None, None, "parent-placeholder", None),
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
    seen = []

    def run(args, **kwargs):
        seen.append(kwargs["env"])
        return SimpleNamespace(returncode=0, stdout=json.dumps({"structured_output": RAW}))

    monkeypatch.setattr(fallbacks.subprocess, "run", run)
    assert fallbacks.ClaudeCliBackend().decide(REQUEST).source == "haiku"
    assert seen[0].get("CLAUDE_CODE_OAUTH_TOKEN") == expected
    assert seen[0]["AGENTIHOOKS_CLASSIFIER_CHILD"] == "1"
    assert seen[0].get("AGENTIHOOKS_ROUTE_ACCOUNT") == route


def test_codex_keeps_native_auth_environment(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_ROUTE_ACCOUNT", "routed")
    monkeypatch.setenv("AH_CC_TOKEN_routed", "parent-placeholder")

    def run(args, **kwargs):
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in kwargs["env"]
        Path(args[args.index("--output-last-message") + 1]).write_text(json.dumps(RAW))
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(fallbacks.subprocess, "run", run)
    assert fallbacks.CodexCliBackend().decide(REQUEST).source == "luna"
