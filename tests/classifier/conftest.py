import os

import pytest


@pytest.fixture(autouse=True)
def _no_live_classifier_children(monkeypatch):
    from hooks.classifier import fallbacks

    def missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(fallbacks.subprocess, "run", missing)
    for name in [name for name in os.environ if name.startswith("AH_CC_TOKEN_")]:
        monkeypatch.delenv(name)
    monkeypatch.delenv("AGENTIHOOKS_ROUTE_ACCOUNT", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-placeholder")
