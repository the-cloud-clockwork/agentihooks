import pytest

from hooks.classifier import ClassifierUnavailable
from scripts.gates import intent
from scripts.swarm import model_pick, priority_sweep, slice_screen


@pytest.fixture(autouse=True)
def isolate_classifier(monkeypatch):
    def unavailable(*args, **kwargs):
        raise ClassifierUnavailable("classifier disabled in unit tests")

    monkeypatch.setattr(model_pick, "decide", unavailable)
    monkeypatch.setattr(slice_screen, "decide", unavailable)
    monkeypatch.setattr(priority_sweep, "decide", unavailable)
    monkeypatch.setattr(priority_sweep.ledger_events, "view", lambda url: None)
    monkeypatch.setattr(intent, "decide", unavailable)
    monkeypatch.setattr(intent, "stamp_body", lambda url, doc, task, run=None: False)
    monkeypatch.setattr(intent, "pr_view", lambda url, run=None: None)
