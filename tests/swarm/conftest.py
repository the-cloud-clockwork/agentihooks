import pytest

from hooks.classifier import Answer, ClassifierUnavailable, DecisionResult
from scripts.gates import intent
from scripts.swarm import model_pick, priority_sweep, profile_choice, slice_screen, trace_plan


@pytest.fixture(autouse=True)
def isolate_classifier(monkeypatch):
    def unavailable(*args, **kwargs):
        raise ClassifierUnavailable("classifier disabled in unit tests")

    def engineer(*args, **kwargs):
        return DecisionResult({"responsibility": Answer("choice", choice="engineer", confidence=1.0)}, "unit-test")

    monkeypatch.setattr(model_pick, "decide", unavailable)
    monkeypatch.setattr(profile_choice, "decide", engineer)
    monkeypatch.setattr(profile_choice, "installed", lambda name: True)
    monkeypatch.setattr(slice_screen, "decide", unavailable)
    monkeypatch.setattr(trace_plan, "decide", unavailable)
    monkeypatch.setattr(priority_sweep, "decide", unavailable)
    monkeypatch.setattr(priority_sweep.ledger_events, "view", lambda url: None)
    monkeypatch.setattr(intent, "decide", unavailable)
    monkeypatch.setattr(intent, "stamp_body", lambda url, doc, task, run=None: False)
    monkeypatch.setattr(intent, "pr_view", lambda url, run=None: None)
