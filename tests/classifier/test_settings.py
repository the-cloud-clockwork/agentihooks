import pytest

from hooks.classifier.settings import Settings, api_configured, load


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("URL", "LITELLM_KEY", "MODELS", "TIMEOUT_S", "DOWN_TTL_S"):
        monkeypatch.delenv(f"AGENTIHOOKS_CLASSIFIER_{name}", raising=False)


def test_defaults():
    assert load() == Settings(
        url="", models=("liquid-d1", "jev-1.13", "pplx-decider-v1-27b"), timeout_s=5.0, down_ttl_s=120.0
    )


def test_overrides(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_URL", " http://litellm:4000 ")
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_MODELS", " jev-1.13 , liquid-d1,")
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_TIMEOUT_S", "2.5")
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_DOWN_TTL_S", "30")
    assert load() == Settings(
        url="http://litellm:4000", models=("jev-1.13", "liquid-d1"), timeout_s=2.5, down_ttl_s=30.0
    )


def test_blank_model_list_keeps_the_default_order(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_MODELS", " , ")
    assert load().models == ("liquid-d1", "jev-1.13", "pplx-decider-v1-27b")


@pytest.mark.parametrize(
    ("url", "key", "configured"),
    [("http://litellm:4000", "sk-x", True), ("", "sk-x", False), ("http://litellm:4000", "", False)],
)
def test_api_needs_both_url_and_key(monkeypatch, url, key, configured):
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_URL", url)
    monkeypatch.setenv("AGENTIHOOKS_CLASSIFIER_LITELLM_KEY", key)
    assert api_configured(load()) is configured
