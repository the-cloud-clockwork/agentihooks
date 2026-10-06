import io
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from hooks.classifier import YesNo, api, cli, core, decision_log, down_cache, fallbacks
from hooks.classifier.core import decide
from hooks.classifier.errors import BackendFailure, ClassifierRequestError, ClassifierUnavailable
from tests.classifier.fakes import FakeUrlopen, http_error, ok

QUESTIONS = {"trivial": YesNo("Simple?", true="yes", false="no")}
RAW = {"answers": {"trivial": {"noul": 0.7}}}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_URL", "http://unused")
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_LITELLM_KEY", "test-placeholder")
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_MODELS", "first,second")


def _lines():
    return [json.loads(line) for line in decision_log.log_path().read_text().splitlines()]


def _child(args, **kwargs):
    if args[0] == "claude":
        return SimpleNamespace(returncode=0, stdout=json.dumps({"structured_output": RAW}))
    Path(args[args.index("--output-last-message") + 1]).write_text(json.dumps(RAW))
    return SimpleNamespace(returncode=0, stdout="")


@pytest.mark.parametrize("harness,source", [("claude", "haiku"), ("codex", "luna")])
def test_default_fallback_after_forced_api_failure(monkeypatch, harness, source):
    fake = FakeUrlopen({"first": http_error(503), "second": http_error(429)})
    monkeypatch.setattr(api, "urlopen", fake)
    monkeypatch.setattr(fallbacks.subprocess, "run", _child)
    result = decide("typo", QUESTIONS, purpose="test", harness=harness)
    assert result.source == source
    assert result.calibrated is False
    assert result.answers["trivial"].noul == 0.7
    first = _lines()[0]
    assert first["failures"] == [
        {"model": "first", "reason": "first: HTTP 503"},
        {"model": "second", "reason": "second: HTTP 429"},
    ]
    assert first["api_down_cached"] is False
    fake.calls.clear()
    decide("typo", QUESTIONS, purpose="cached", harness=harness)
    assert not fake.calls
    assert _lines()[1]["api_down_cached"] is True
    assert _lines()[1]["failures"] == first["failures"]


@pytest.mark.parametrize(
    "failure,reason",
    [
        (TimeoutError(), "timeout"),
        (URLError(TimeoutError()), "timeout"),
        (URLError("private"), "connection error"),
        (b"private invalid json", "parse error"),
        ({"answers": []}, "parse error"),
        ({"answers": {"trivial": None}}, "parse error"),
    ],
)
def test_api_failure_reason_is_logged(monkeypatch, failure, reason):
    if isinstance(failure, bytes):
        failure = io.BytesIO(failure)
    elif isinstance(failure, dict):
        failure = io.BytesIO(json.dumps(failure).encode())
    monkeypatch.setattr(api, "urlopen", FakeUrlopen({"first": failure, "second": ok()}))
    assert decide("typo", QUESTIONS, purpose="test").source == "second"
    actual = _lines()[0]["failures"][0]["reason"]
    assert actual.startswith(f"first: {reason}")
    if reason != "parse error":
        assert actual == f"first: {reason}"
    assert "private" not in json.dumps(_lines())


def test_request_error_is_logged_without_fallback(monkeypatch):
    monkeypatch.setattr(api, "urlopen", FakeUrlopen({"first": http_error(400, "bad request test-placeholder")}))
    with pytest.raises(ClassifierRequestError):
        decide("typo", QUESTIONS, purpose="request")
    assert "HTTP 400" in _lines()[0]["failures"][0]["reason"]
    assert "test-placeholder" not in json.dumps(_lines())
    assert not down_cache.is_down(120)


def test_missing_preferred_cli_tries_other(monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_CLASSIFIER_URL")
    seen = []

    def run(args, **kwargs):
        seen.append(args[0])
        if args[0] == "codex":
            raise FileNotFoundError
        return _child(args, **kwargs)

    monkeypatch.setattr(fallbacks.subprocess, "run", run)
    assert decide("typo", QUESTIONS, purpose="missing", harness="codex").source == "haiku"
    assert seen == ["codex", "claude"]
    assert _lines()[0]["failures"] == [{"model": "gpt-6-luna", "reason": "CLI missing"}]


def test_both_missing_raises_with_failure_log(monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_CLASSIFIER_URL")
    with pytest.raises(ClassifierUnavailable):
        decide("typo", QUESTIONS, purpose="missing", harness="claude")
    assert _lines()[0]["failures"] == [
        {"model": "haiku", "reason": "CLI missing"},
        {"model": "gpt-6-luna", "reason": "CLI missing"},
    ]
    assert _lines()[0]["source"] is None


def test_empty_explicit_fallbacks_do_not_launch_children(monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_CLASSIFIER_URL")
    monkeypatch.setattr(fallbacks.subprocess, "run", lambda *a, **k: pytest.fail("unexpected child"))
    with pytest.raises(ClassifierUnavailable):
        decide("typo", QUESTIONS, purpose="explicit", fallbacks=[])
    assert _lines()[0]["failures"] == []


def test_custom_failure_reason_redacts_key(monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_CLASSIFIER_URL")

    class Failed:
        name = "model"

        def decide(self, request):
            raise BackendFailure("test-placeholder refused")

    with pytest.raises(ClassifierUnavailable):
        decide("typo", QUESTIONS, purpose="private", fallbacks=[Failed()])
    assert _lines()[0]["failures"] == [{"model": "model", "reason": "[redacted] refused"}]


def test_classify_harness_reaches_decide(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("AGENTIHOOKS_CLASSIFIER_URL")
    monkeypatch.setattr(fallbacks.subprocess, "run", _child)
    state, questions = tmp_path / "state.json", tmp_path / "questions.json"
    state.write_text('"typo"')
    questions.write_text(
        json.dumps({"trivial": {"type": "noul", "instructions": "q", "criteria": {"true": "t", "false": "f"}}})
    )
    assert cli.classify_main(["--state", str(state), "--questions", str(questions), "--harness", "codex"]) == 0
    assert json.loads(capsys.readouterr().out)["source"] == "luna"


@pytest.mark.parametrize("contents", [None, "", "not json"])
def test_legacy_down_marker_keeps_cache_and_does_not_break_fallback(monkeypatch, contents):
    down_cache.mark_down()
    if contents is None:
        down_cache.clear()
    else:
        down_cache.marker_path().write_text(contents)
    assert down_cache.failures() == []


@pytest.mark.parametrize(
    "catalog,override,expected",
    [
        ({"models": [{"slug": "catalog-luna"}]}, None, "catalog-luna"),
        ({"models": [{"slug": "catalog-luna"}]}, "override-luna", "override-luna"),
    ],
)
def test_failed_codex_logs_selected_slug(monkeypatch, tmp_path, catalog, override, expected):
    monkeypatch.delenv("AGENTIHOOKS_CLASSIFIER_URL")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "models_cache.json").write_text(json.dumps(catalog))
    if override:
        monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_LUNA_MODEL", override)
    else:
        monkeypatch.delenv("AGENTIHOOKS_CLASSIFIER_LUNA_MODEL", raising=False)
    with pytest.raises(ClassifierUnavailable):
        core.decide("typo", QUESTIONS, purpose="model", harness="codex")
    assert _lines()[0]["failures"][0] == {"model": expected, "reason": "CLI missing"}


def test_redaction_without_key_does_not_replace_text(monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_CLASSIFIER_LITELLM_KEY")
    assert decision_log.failure_record("XXXX", BackendFailure("XXXX refused")) == {
        "model": "XXXX",
        "reason": "XXXX refused",
    }


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_harness_help_and_valid_choices(monkeypatch, tmp_path, capsys, harness):
    monkeypatch.delenv("AGENTIHOOKS_CLASSIFIER_URL")
    monkeypatch.setattr(fallbacks.subprocess, "run", _child)
    state, questions = tmp_path / "state", tmp_path / "questions"
    state.write_text('"typo"')
    questions.write_text(
        json.dumps({"trivial": {"type": "noul", "instructions": "q", "criteria": {"true": "t", "false": "f"}}})
    )
    args = ["--state", str(state), "--questions", str(questions)]
    assert cli.classify_main([*args, "--harness", harness]) == 0
    with pytest.raises(SystemExit) as error:
        cli.classify_main([*args, "--harness", "invalid"])
    assert error.value.code == 2
    with pytest.raises(SystemExit) as help_exit:
        cli.classify_main(["--help"])
    assert help_exit.value.code == 0
    assert any(line.rstrip().endswith("  CLI fallback target") for line in capsys.readouterr().out.splitlines())
