import json
import os
import time

import pytest

from hooks import config
from hooks.classifier import (
    ClassifierInputError,
    ClassifierRequestError,
    ClassifierUnavailable,
    YesNo,
    api,
    decide,
    down_cache,
)
from hooks.classifier.errors import BackendFailure
from hooks.classifier.result import Answer, DecisionResult
from hooks.classifier.settings import DEFAULT_MODELS, load
from tests.classifier.fakes import KEY, FakeUrlopen, http_error, ok

QUESTIONS = {"trivial": YesNo("One line?", true="t", false="f")}
ALL_OK = {model: ok() for model in DEFAULT_MODELS}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_URL", "http://litellm:4000")
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_LITELLM_KEY", KEY)
    for name in ("MODELS", "TIMEOUT_S", "DOWN_TTL_S"):
        monkeypatch.delenv(f"AGENTIHOOKS_CLASSIFIER_{name}", raising=False)


def _wire(monkeypatch, script):
    fake = FakeUrlopen(script)
    monkeypatch.setattr(api, "urlopen", fake)
    return fake


def _log_lines():
    path = config.AGENTIHOOKS_HOME / "classifier" / "decisions.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class FakeFallback:
    name = "haiku"

    def __init__(self, outcome=None):
        self.outcome = outcome
        self.calls = 0
        self.requests = []

    def decide(self, request):
        self.calls += 1
        self.requests.append(request)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return DecisionResult(answers={"trivial": Answer("noul", noul=0.6)}, source=self.name, calibrated=False)


def test_first_model_answers(monkeypatch):
    fake = _wire(monkeypatch, ALL_OK)
    result = decide("typo", QUESTIONS, purpose="test")
    assert result.source == "liquid-d1"
    assert [c["model"] for c in fake.calls] == ["liquid-d1"]
    assert fake.calls[0]["timeout"] == 5.0
    assert result.answers["trivial"].noul == 0.74
    assert result.latency_ms >= 0


def test_model_order_follows_the_variable(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_MODELS", "jev-1.13,liquid-d1")
    fake = _wire(monkeypatch, ALL_OK)
    assert decide("typo", QUESTIONS, purpose="test").source == "jev-1.13"
    assert [c["model"] for c in fake.calls] == ["jev-1.13"]


@pytest.mark.parametrize("failure", [http_error(429), http_error(503), TimeoutError(), http_error(400, "context")])
def test_failover_moves_to_the_next_model(monkeypatch, failure):
    fake = _wire(monkeypatch, {**ALL_OK, "liquid-d1": failure})
    assert decide("typo", QUESTIONS, purpose="test").source == "jev-1.13"
    assert [c["model"] for c in fake.calls] == ["liquid-d1", "jev-1.13"]


def test_auth_failure_skips_every_api_model_to_the_fallback(monkeypatch):
    fake = _wire(monkeypatch, {**ALL_OK, "liquid-d1": http_error(401)})
    fallback = FakeFallback()
    result = decide("typo", QUESTIONS, purpose="test", fallbacks=[fallback])
    assert [c["model"] for c in fake.calls] == ["liquid-d1"]
    assert (result.source, result.calibrated, fallback.calls) == ("haiku", False, 1)


def test_caller_bug_400_raises_and_never_falls_back(monkeypatch):
    fake = _wire(monkeypatch, {**ALL_OK, "liquid-d1": http_error(400, "invalid_union")})
    fallback = FakeFallback()
    with pytest.raises(ClassifierRequestError):
        decide("typo", QUESTIONS, purpose="test", fallbacks=[fallback])
    assert len(fake.calls) == 1 and fallback.calls == 0
    assert not down_cache.is_down(120)


def test_invalid_input_raises_before_any_call(monkeypatch):
    fake = _wire(monkeypatch, ALL_OK)
    fallback = FakeFallback()
    with pytest.raises(ClassifierInputError):
        decide("typo", {"Bad": YesNo("q", true="t", false="f")}, purpose="test", fallbacks=[fallback])
    assert fake.calls == [] and fallback.calls == 0


def test_large_state_skips_models_whose_context_is_too_small(monkeypatch):
    fake = _wire(monkeypatch, ALL_OK)
    result = decide("x" * 4 * 40_000, QUESTIONS, purpose="test")
    assert result.source == "pplx-decider-v1-27b"
    assert [c["model"] for c in fake.calls] == ["pplx-decider-v1-27b"]


def test_state_that_just_fits_keeps_the_small_model(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_MODELS", "jev-1.13")
    _wire(monkeypatch, ALL_OK)
    from hooks.classifier.core import api_backends
    from hooks.classifier.result import DecisionRequest

    overhead = DecisionRequest("", QUESTIONS).estimated_tokens()
    fits = DecisionRequest("x" * 4 * (32_768 - overhead - 2), QUESTIONS)
    too_big = DecisionRequest("x" * 4 * (32_768 - overhead + 2), QUESTIONS)
    assert [b.name for b in api_backends(fits, load())] == ["jev-1.13"]
    assert api_backends(too_big, load()) == []


def test_unknown_model_is_never_skipped_by_context(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_MODELS", "future-decider")
    _wire(monkeypatch, {"future-decider": ok()})
    assert decide("x" * 4 * 300_000, QUESTIONS, purpose="test").source == "future-decider"


def test_all_api_models_down_marks_the_down_cache(monkeypatch):
    fake = _wire(monkeypatch, {m: http_error(503) for m in DEFAULT_MODELS})
    with pytest.raises(ClassifierUnavailable):
        decide("typo", QUESTIONS, purpose="test")
    assert len(fake.calls) == 3
    assert down_cache.is_down(120)
    fake.calls.clear()
    fallback = FakeFallback()
    assert decide("typo", QUESTIONS, purpose="test", fallbacks=[fallback]).source == "haiku"
    assert fake.calls == []


def test_down_cache_expires_after_its_ttl(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_DOWN_TTL_S", "60")
    down_cache.mark_down()
    marker = config.AGENTIHOOKS_HOME / "classifier" / "api-down"
    old = time.time() - 61
    os.utime(marker, (old, old))
    assert not down_cache.is_down(60)
    fake = _wire(monkeypatch, ALL_OK)
    assert decide("typo", QUESTIONS, purpose="test").source == "liquid-d1"
    assert len(fake.calls) == 1


def test_down_cache_holds_inside_its_ttl():
    down_cache.mark_down()
    marker = config.AGENTIHOOKS_HOME / "classifier" / "api-down"
    recent = time.time() - 59
    os.utime(marker, (recent, recent))
    assert down_cache.is_down(60)
    assert not down_cache.is_down(0)


def test_auth_failure_marks_the_down_cache(monkeypatch):
    _wire(monkeypatch, {**ALL_OK, "liquid-d1": http_error(403)})
    with pytest.raises(ClassifierUnavailable):
        decide("typo", QUESTIONS, purpose="test")
    assert down_cache.is_down(120)


def test_failure_of_only_the_large_context_model_leaves_the_api_up(monkeypatch):
    fake = _wire(monkeypatch, {**ALL_OK, "pplx-decider-v1-27b": http_error(404)})
    with pytest.raises(ClassifierUnavailable):
        decide("x" * 4 * 40_000, QUESTIONS, purpose="test")
    assert not down_cache.is_down(120)
    assert decide("typo", QUESTIONS, purpose="test").source == "liquid-d1"
    assert [c["model"] for c in fake.calls] == ["pplx-decider-v1-27b", "liquid-d1"]


def test_refused_key_on_a_large_input_marks_the_down_cache(monkeypatch):
    _wire(monkeypatch, {**ALL_OK, "pplx-decider-v1-27b": http_error(401)})
    with pytest.raises(ClassifierUnavailable):
        decide("x" * 4 * 40_000, QUESTIONS, purpose="test")
    assert down_cache.is_down(120)
    assert [f["model"] for f in down_cache.failures()] == ["pplx-decider-v1-27b"]


def test_context_skip_of_every_model_is_not_an_outage(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_MODELS", "jev-1.13")
    _wire(monkeypatch, ALL_OK)
    with pytest.raises(ClassifierUnavailable):
        decide("x" * 4 * 40_000, QUESTIONS, purpose="test")
    assert not down_cache.is_down(120)


@pytest.mark.parametrize("unset", ["AGENTIHOOKS_CLASSIFIER_URL", "AGENTIHOOKS_CLASSIFIER_LITELLM_KEY"])
def test_unset_url_or_key_goes_straight_to_the_fallback(monkeypatch, unset):
    monkeypatch.delenv(unset)
    fake = _wire(monkeypatch, ALL_OK)
    fallback = FakeFallback()
    assert decide("typo", QUESTIONS, purpose="test", fallbacks=[fallback]).source == "haiku"
    assert fake.calls == []
    assert not down_cache.is_down(120)


def test_failing_fallback_moves_to_the_next(monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_CLASSIFIER_URL")
    first = FakeFallback(BackendFailure("claude missing"))
    second = FakeFallback()
    second.name = "luna"
    assert decide("typo", QUESTIONS, purpose="test", fallbacks=[first, second]).source == "luna"
    assert (first.calls, second.calls) == (1, 1)


def test_fallback_receives_the_request(monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_CLASSIFIER_URL")
    fallback = FakeFallback()
    decide({"task": "typo"}, QUESTIONS, purpose="test", fallbacks=[fallback])
    (request,) = fallback.requests
    assert (request.state, request.questions) == ({"task": "typo"}, QUESTIONS)


def test_latency_is_measured_in_milliseconds_and_logged(monkeypatch):
    from hooks.classifier import core

    clock = iter([10.0, 11.5])
    monkeypatch.setattr(core.time, "monotonic", lambda: next(clock))
    _wire(monkeypatch, ALL_OK)
    assert decide("typo", QUESTIONS, purpose="test").latency_ms == 1500
    (line,) = _log_lines()
    assert line["latency_ms"] == 1500


def test_nothing_reachable_raises_unavailable_and_logs_it(monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_CLASSIFIER_URL")
    with pytest.raises(ClassifierUnavailable) as err:
        decide("typo", QUESTIONS, purpose="gate", fallbacks=[FakeFallback(BackendFailure("x"))])
    assert str(err.value) == "no decision backend answered"
    (line,) = _log_lines()
    assert (line["purpose"], line["source"], line["answers"]) == ("gate", None, {})


def test_decision_log_line_shape(monkeypatch):
    _wire(monkeypatch, ALL_OK)
    decide({"task": "typo"}, QUESTIONS, purpose="model-pick")
    (line,) = _log_lines()
    assert set(line) == {
        "ts",
        "purpose",
        "source",
        "calibrated",
        "latency_ms",
        "cost",
        "answers",
        "state_digest",
        "failures",
        "api_down_cached",
        "definition",
        "definition_digest",
        "expected",
    }
    assert line["purpose"] == "model-pick"
    assert line["source"] == "liquid-d1"
    assert line["calibrated"] is True
    assert line["cost"] == 0.000012
    assert line["answers"] == {"trivial": {"type": "noul", "noul": 0.74}}
    assert len(line["state_digest"]) == 16
    decide({"task": "typo"}, QUESTIONS, purpose="model-pick")
    decide({"task": "other"}, QUESTIONS, purpose="model-pick")
    digests = [entry["state_digest"] for entry in _log_lines()]
    assert digests[0] == digests[1] != digests[2]


def test_key_never_reaches_the_log_or_an_error(monkeypatch, caplog):
    _wire(monkeypatch, {**ALL_OK, "liquid-d1": http_error(401, f"bad {KEY}"), "jev-1.13": ok()})
    with pytest.raises(ClassifierUnavailable) as err:
        decide({"task": "typo"}, QUESTIONS, purpose="test")
    _wire(monkeypatch, {**ALL_OK, "liquid-d1": http_error(400, f"invalid {KEY}")})
    down_cache.clear()
    with pytest.raises(ClassifierRequestError) as bug:
        decide({"task": "typo"}, QUESTIONS, purpose="test")
    _wire(monkeypatch, ALL_OK)
    decide({"task": "typo"}, QUESTIONS, purpose="test")
    log_text = (config.AGENTIHOOKS_HOME / "classifier" / "decisions.jsonl").read_text()
    for text in (str(err.value), str(bug.value), log_text, caplog.text):
        assert KEY not in text
