import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from hooks.classifier import YesNo
from hooks.classifier.errors import BackendFailure
from hooks.classifier.fallbacks import ClaudeCliBackend, CodexCliBackend, cli_backends, luna_model
from hooks.classifier.result import DecisionRequest

REQUEST = DecisionRequest({"task": "typo"}, {"simple": YesNo("Simple?", true="yes", false="no")})
OUTPUT = {"answers": {"simple": {"noul": 0.9}}}


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_CLASSIFIER_LUNA_MODEL", raising=False)
    monkeypatch.delenv("AGENTIHOOKS_CLASSIFIER_FALLBACK_TIMEOUT_S", raising=False)


@pytest.mark.parametrize("backend", [ClaudeCliBackend, CodexCliBackend])
def test_cli_contract_and_result(monkeypatch, backend):
    from hooks.classifier import fallbacks

    monkeypatch.setenv("CLAUDECODE", "parent")
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_FALLBACK_TIMEOUT_S", "17.5")
    seen = []

    def run(args, **kwargs):
        seen.append((args, kwargs))
        assert kwargs["env"]["AGENTIHOOKS_CLASSIFIER_CHILD"] == "1"
        assert "CLAUDECODE" not in kwargs["env"]
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["timeout"] == 17.5
        assert json.loads(kwargs["input"])["state"] == REQUEST.state
        assert json.loads(kwargs["input"])["questions"] == REQUEST.wire()["questions"]
        if args[0] == "claude":
            assert args[:5] == ["claude", "-p", "--model", "haiku", "--output-format"]
            assert args[args.index("--output-format") + 1] == "json"
            assert args[args.index("--tools") + 1] == ""
            assert "--no-session-persistence" in args
            assert "--system-prompt" in args
            schema = json.loads(args[args.index("--json-schema") + 1])
            output = json.dumps({"structured_output": OUTPUT})
        else:
            assert args[:5] == ["codex", "--no-daemon", "exec", "-m", "gpt-6-luna"]
            assert 'model_reasoning_effort="low"' in args
            assert args[args.index("--sandbox") + 1] == "read-only"
            assert "--skip-git-repo-check" in args
            schema = json.loads(Path(args[args.index("--output-schema") + 1]).read_text())
            Path(args[args.index("--output-last-message") + 1]).write_text(json.dumps(OUTPUT))
            output = "progress output is ignored"
        assert schema["properties"]["answers"]["required"] == ["simple"]
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr(fallbacks.subprocess, "run", run)
    result = backend().decide(REQUEST)
    assert result.source == ("haiku" if backend is ClaudeCliBackend else "luna")
    assert result.calibrated is False
    assert result.answers["simple"].noul == 0.9
    assert len(seen) == 1
    assert not Path(seen[0][1]["cwd"]).exists()


@pytest.mark.parametrize("backend", [ClaudeCliBackend, CodexCliBackend])
@pytest.mark.parametrize(
    "failure,reason",
    [
        (FileNotFoundError(), "CLI missing"),
        (subprocess.TimeoutExpired("cli", 1), "timeout"),
        (OSError("private"), "CLI failed"),
    ],
)
def test_transport_failure_is_safe(monkeypatch, backend, failure, reason):
    from hooks.classifier import fallbacks

    def run(*args, **kwargs):
        raise failure

    monkeypatch.setattr(fallbacks.subprocess, "run", run)
    with pytest.raises(BackendFailure, match=reason) as error:
        backend().decide(REQUEST)
    assert "private" not in str(error.value)


@pytest.mark.parametrize(
    "output,code,reason",
    [
        ("private output", 7, "CLI status 7"),
        ("private output", 0, "parse error"),
        (json.dumps({"is_error": True, "result": "private"}), 0, "CLI error"),
        (json.dumps({"result": "not json"}), 0, "parse error"),
        (json.dumps({"structured_output": {}}), 0, "parse error"),
    ],
)
def test_bad_claude_output_is_safe(monkeypatch, output, code, reason):
    from hooks.classifier import fallbacks

    monkeypatch.setattr(fallbacks.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=code, stdout=output))
    with pytest.raises(BackendFailure, match=reason) as error:
        ClaudeCliBackend().decide(REQUEST)
    assert "private" not in str(error.value)


def test_claude_accepts_json_result_string(monkeypatch):
    from hooks.classifier import fallbacks

    monkeypatch.setattr(
        fallbacks.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps({"result": json.dumps(OUTPUT)})),
    )
    assert ClaudeCliBackend().decide(REQUEST).answers["simple"].noul == 0.9


def test_codex_missing_output_is_parse_error(monkeypatch):
    from hooks.classifier import fallbacks

    monkeypatch.setattr(fallbacks.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=""))
    with pytest.raises(BackendFailure, match="parse error"):
        CodexCliBackend().decide(REQUEST)


def test_luna_catalog_and_override(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    path = tmp_path / "models_cache.json"
    path.write_text(json.dumps({"models": [{"slug": "gpt-6-sol"}, {"slug": "gpt-6-luna-2026"}]}))
    assert luna_model() == "gpt-6-luna-2026"
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_LUNA_MODEL", "custom-luna")
    assert luna_model() == "custom-luna"


@pytest.mark.parametrize("contents", [None, "invalid", "{}", '{"models":[{"slug":"gpt-6-sol"}]}'])
def test_luna_catalog_default(monkeypatch, tmp_path, contents):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    if contents is not None:
        (tmp_path / "models_cache.json").write_text(contents)
    assert luna_model() == "gpt-6-luna"


@pytest.mark.parametrize(
    "target,harness,expected",
    [
        ("claude", None, ["haiku", "luna"]),
        ("codex", None, ["luna", "haiku"]),
        ("", None, ["haiku", "luna"]),
        ("codex", "claude", ["haiku", "luna"]),
        ("claude", "codex", ["luna", "haiku"]),
    ],
)
def test_fallback_order(monkeypatch, target, harness, expected):
    monkeypatch.setenv("AGENTIHOOKS_TARGET", target)
    assert [b.name for b in cli_backends(harness)] == expected
