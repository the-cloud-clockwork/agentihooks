import json
import socket
import urllib.error

import pytest

from hooks.classifier import Choice, ClassifierRequestError, Score, YesNo, api
from hooks.classifier.errors import BackendFailure
from hooks.classifier.result import DecisionRequest
from tests.classifier.fakes import KEY, FakeUrlopen, http_error, ok, payload

QUESTIONS = {
    "tier": Choice("Which tier?", {"small": "s", "large": "l"}),
    "effort": Score("How much?", ["low", "high"]),
    "trivial": YesNo("One line?", true="t", false="f"),
}


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_LITELLM_KEY", KEY)


def _backend(monkeypatch, outcome, model="pplx-decider-v1-27b"):
    fake = FakeUrlopen({model: outcome})
    monkeypatch.setattr(api, "urlopen", fake)
    return api.DecisionsApiBackend(model, "http://litellm:4000/", 5.0), fake


def test_request_body_headers_and_route(monkeypatch):
    backend, fake = _backend(monkeypatch, ok())
    backend.decide(DecisionRequest({"task": "typo"}, QUESTIONS))
    call = fake.calls[0]
    assert call["url"] == "http://litellm:4000/v1/decisions"
    assert call["timeout"] == 5.0
    assert call["headers"]["Authorization"] == f"Bearer {KEY}"
    assert call["headers"]["Content-type"] == "application/json"
    assert call["body"] == {
        "model": "pplx-decider-v1-27b",
        "state": {"task": "typo"},
        "questions": {
            "tier": {"type": "choice", "instructions": "Which tier?", "criteria": {"small": "s", "large": "l"}},
            "effort": {"type": "score", "instructions": "How much?", "criteria": ["low", "high"]},
            "trivial": {"type": "noul", "instructions": "One line?", "criteria": {"true": "t", "false": "f"}},
        },
    }


def test_key_is_read_at_call_time(monkeypatch):
    backend, fake = _backend(monkeypatch, ok())
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_LITELLM_KEY", "sk-rotated")
    backend.decide(DecisionRequest("s", QUESTIONS))
    assert fake.calls[0]["headers"]["Authorization"] == "Bearer sk-rotated"


def test_response_parses_each_type(monkeypatch):
    backend, _ = _backend(monkeypatch, ok())
    result = backend.decide(DecisionRequest("s", QUESTIONS))
    tier, effort, trivial = result.answers["tier"], result.answers["effort"], result.answers["trivial"]
    assert (tier.type, tier.choice, tier.confidence) == ("choice", "small", 0.9)
    assert tier.probabilities == {"small": 0.95, "large": 0.05}
    assert (effort.type, effort.score, effort.confidence) == ("score", 0.2, 0.8)
    assert effort.legend == {"0": "low", "1": "high"}
    assert effort.probabilities == {"0": 0.8, "1": 0.2}
    assert (trivial.type, trivial.noul) == ("noul", 0.74)
    assert result.source == "pplx-decider-v1-27b"
    assert result.calibrated is True
    assert result.cost == 0.000012


def test_answer_missing_from_response_moves_on(monkeypatch):
    body = payload()
    del body["answers"]["trivial"]
    backend, _ = _backend(monkeypatch, ok(body))
    with pytest.raises(BackendFailure, match="trivial") as err:
        backend.decide(DecisionRequest("s", QUESTIONS))
    assert err.value.skip_api is False


def test_non_json_response_moves_on(monkeypatch):
    from tests.classifier.fakes import Response

    backend, _ = _backend(monkeypatch, Response(b"<html>gateway</html>"))
    with pytest.raises(BackendFailure):
        backend.decide(DecisionRequest("s", QUESTIONS))


@pytest.mark.parametrize("code", [429, 500, 502, 503, 404])
def test_retryable_status_moves_on(monkeypatch, code):
    backend, _ = _backend(monkeypatch, http_error(code))
    with pytest.raises(BackendFailure, match=str(code)) as err:
        backend.decide(DecisionRequest("s", QUESTIONS))
    assert err.value.skip_api is False


@pytest.mark.parametrize("code", [401, 403])
def test_auth_failure_skips_every_api_model(monkeypatch, code):
    backend, _ = _backend(monkeypatch, http_error(code, f"Invalid key {KEY}"))
    with pytest.raises(BackendFailure) as err:
        backend.decide(DecisionRequest("s", QUESTIONS))
    assert err.value.skip_api is True
    assert KEY not in str(err.value)


@pytest.mark.parametrize("message", ["context length exceeded", "too many input tokens", "Max Tokens reached"])
def test_400_naming_context_or_tokens_moves_on(monkeypatch, message):
    backend, _ = _backend(monkeypatch, http_error(400, message))
    with pytest.raises(BackendFailure) as err:
        backend.decide(DecisionRequest("s", QUESTIONS))
    assert err.value.skip_api is False


def test_other_400_is_a_caller_bug_with_the_key_redacted(monkeypatch):
    backend, _ = _backend(monkeypatch, http_error(400, f"invalid_union on questions.tier sent with {KEY}"))
    with pytest.raises(ClassifierRequestError, match="invalid_union") as err:
        backend.decide(DecisionRequest("s", QUESTIONS))
    assert KEY not in str(err.value)


def test_400_with_unreadable_body_is_a_caller_bug(monkeypatch):
    import io

    error = urllib.error.HTTPError("http://x", 400, "Bad Request", {}, io.BytesIO(b"not json"))
    backend, _ = _backend(monkeypatch, error)
    with pytest.raises(ClassifierRequestError, match="not json"):
        backend.decide(DecisionRequest("s", QUESTIONS))


@pytest.mark.parametrize(
    "error",
    [urllib.error.URLError("refused"), TimeoutError("timed out"), socket.timeout("slow"), ConnectionResetError()],
)
def test_transport_errors_move_on(monkeypatch, error):
    backend, _ = _backend(monkeypatch, error)
    with pytest.raises(BackendFailure) as err:
        backend.decide(DecisionRequest("s", QUESTIONS))
    assert err.value.skip_api is False


def test_estimated_tokens_is_four_characters_per_token():
    request = DecisionRequest("x" * 400, {"a": YesNo("q", true="t", false="f")})
    wire = json.dumps(
        {
            "state": "x" * 400,
            "questions": {"a": {"type": "noul", "instructions": "q", "criteria": {"true": "t", "false": "f"}}},
        }
    )
    assert request.estimated_tokens() == len(wire) // 4
