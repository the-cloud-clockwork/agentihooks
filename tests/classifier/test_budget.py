import pytest

from hooks.classifier import YesNo, api, decide
from hooks.classifier.result import DecisionRequest
from tests.classifier.fakes import KEY, FakeUrlopen, ok

QUESTIONS = {"trivial": YesNo("One line?", true="t", false="f")}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_URL", "http://litellm:4000")
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_LITELLM_KEY", KEY)
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_MODELS", "pplx-decider-v1-27b")
    monkeypatch.delenv("AGENTIHOOKS_CLASSIFIER_TIMEOUT_S", raising=False)


def _budget(monkeypatch, state):
    fake = FakeUrlopen({"pplx-decider-v1-27b": ok()})
    monkeypatch.setattr(api, "urlopen", fake)
    decide(state, QUESTIONS, purpose="test")
    return fake.calls[0]["timeout"]


def test_a_small_input_keeps_the_configured_timeout(monkeypatch):
    assert _budget(monkeypatch, "typo") == 5.0


def test_the_budget_starts_growing_past_the_tokens_the_timeout_covers(monkeypatch):
    overhead = DecisionRequest("", QUESTIONS).estimated_tokens()
    assert _budget(monkeypatch, "x" * 4 * (35_000 - overhead)) == 5.0
    assert _budget(monkeypatch, "x" * 4 * (35_700 - overhead)) == 5.1


def test_seventy_thousand_tokens_get_a_budget_above_their_seven_second_answer(monkeypatch):
    state = "x" * 4 * 70_000
    tokens = DecisionRequest(state, QUESTIONS).estimated_tokens()
    budget = _budget(monkeypatch, state)
    assert budget == tokens / 7_000
    assert budget > 7.0


def test_the_budget_is_capped(monkeypatch):
    assert _budget(monkeypatch, "x" * 4 * 250_000) == 30.0


def test_a_configured_timeout_above_the_cap_is_kept(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_TIMEOUT_S", "45")
    assert _budget(monkeypatch, "x" * 4 * 250_000) == 45.0
    assert _budget(monkeypatch, "typo") == 45.0
