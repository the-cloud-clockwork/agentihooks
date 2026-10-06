import pytest

from hooks.classifier import ClassifierUnavailable
from scripts.swarm import model_pick, slice_screen


@pytest.fixture(autouse=True)
def isolate_classifier(monkeypatch):
    def unavailable(*args, **kwargs):
        raise ClassifierUnavailable("classifier disabled in unit tests")

    monkeypatch.setattr(model_pick, "decide", unavailable)
    monkeypatch.setattr(slice_screen, "decide", unavailable)
