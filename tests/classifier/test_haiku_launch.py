import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from hooks.classifier import YesNo, fallbacks
from hooks.classifier.errors import BackendFailure
from hooks.classifier.result import DecisionRequest

REQUEST = DecisionRequest("typo", {"simple": YesNo("Small?", true="yes", false="no")})
RAW = {"answers": {"simple": {"noul": 1}}}
ROUTED = "[agenti] account=other routing_left=70% 5h_left=80% 7d_left=70% source=cached\n"


def _launcher(seen, report=None, code=0, stdout=ROUTED + json.dumps({"structured_output": RAW}) + "\n"):
    def run(args, **kwargs):
        seen.append((args, kwargs))
        assert json.loads((Path(kwargs["cwd"]) / "request.json").read_text()) == REQUEST.wire()
        if report is not None:
            Path(args[args.index("--agentihooks-report") + 1]).write_text(report)
        return SimpleNamespace(returncode=code, stdout=stdout)

    return run


@pytest.mark.parametrize("route", [None, "routed"])
@pytest.mark.parametrize("parent_token", [None, "parent-placeholder"])
def test_haiku_launches_through_agentihooks_claude(monkeypatch, route, parent_token):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")
    if parent_token:
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", parent_token)
    if route:
        monkeypatch.setenv("AGENTIHOOKS_ROUTE_ACCOUNT", route)
    seen = []
    monkeypatch.setattr(fallbacks.subprocess, "run", _launcher(seen))
    assert fallbacks.ClaudeCliBackend().decide(REQUEST).source == "haiku"
    args, kwargs = seen[0]
    wire = Path(kwargs["cwd"]) / "request.json"
    assert args[:3] == ["bash", "-lic", f'exec "$0" "$@" < {wire}']
    assert args[3:6] == ["agentihooks", "claude", "--agentihooks-report"]
    assert Path(args[6]).parent == Path(kwargs["cwd"])
    assert args[7 : args.index("-p")] == (["--route", route] if route else [])
    assert args[args.index("-p") :][:3] == ["-p", "--model", "haiku"]
    assert args[args.index("--system-prompt") + 1] == fallbacks.PROMPT
    assert kwargs["env"].get("CLAUDE_CODE_OAUTH_TOKEN") == os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    assert not hasattr(fallbacks, "select_credential")


def test_unroutable_account_fails_naming_why(monkeypatch):
    seen = []
    report = "status=failed\nerror=no non-empty AH_CC_TOKEN_* variables found\n"
    monkeypatch.setattr(fallbacks.subprocess, "run", _launcher(seen, report=report, code=3, stdout=""))
    with pytest.raises(BackendFailure, match=r"^no Claude account: no non-empty AH_CC_TOKEN_\* variables found$"):
        fallbacks.ClaudeCliBackend().decide(REQUEST)
    assert len(seen) == 1


@pytest.mark.parametrize("report", [None, "status=routed\naccount=other\nplacement=open\n"])
def test_failure_without_route_refusal_keeps_cli_status(monkeypatch, report):
    monkeypatch.setattr(fallbacks.subprocess, "run", _launcher([], report=report, code=1, stdout=""))
    with pytest.raises(BackendFailure, match="^CLI status 1$"):
        fallbacks.ClaudeCliBackend().decide(REQUEST)


def test_codex_keeps_native_auth_environment(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")

    def run(args, **kwargs):
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in kwargs["env"]
        Path(args[args.index("--output-last-message") + 1]).write_text(json.dumps(RAW))
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(fallbacks.subprocess, "run", run)
    assert fallbacks.CodexCliBackend().decide(REQUEST).source == "luna"
