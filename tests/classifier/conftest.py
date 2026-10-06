import pytest


@pytest.fixture(autouse=True)
def _no_live_classifier_children(monkeypatch):
    from hooks.classifier import fallbacks

    def missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(fallbacks.subprocess, "run", missing)
