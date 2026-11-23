import argparse
from importlib import import_module
from typing import Any
from unittest.mock import Mock, call

import pytest

from scripts.gates.base import Who
from scripts.swarm.store import AgentRecord, SwarmError
from tests.review.fakes import HEAD, SLUG, URL, store

AUTHOR = "engineer@a1b2c3-0001"


@pytest.mark.feature("review-queue-gate")
@pytest.mark.parametrize("stop", ["", "intent", "review"])
def test_queue_admission_preserves_intent_review_and_freshness_order(
    monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    gate = import_module("scripts.review.gate")
    found, args, caller = store(), argparse.Namespace(slug=SLUG, url=URL), AgentRecord(AUTHOR, "eng", "t1")
    events = Mock()
    intent, review, freshness = Mock(), Mock(), Mock(return_value=None)
    events.attach_mock(intent, "intent")
    events.attach_mock(review, "review")
    events.attach_mock(freshness, "freshness")
    if stop:
        getattr(events, stop).side_effect = SwarmError(f"{stop} refused")
    monkeypatch.setattr(gate, "admit", review)
    monkeypatch.setattr(gate.queue_exit, "require_fresh", freshness)
    if stop:
        with pytest.raises(SwarmError, match=f"{stop} refused"):
            gate.queue_admit(found, args, caller, caller, HEAD, intent, None)
    else:
        assert gate.queue_admit(found, args, caller, caller, HEAD, intent, None) is None
    expected: list[Any] = [call.intent(found, args, caller, HEAD)]
    if stop != "intent":
        expected.append(call.review(found, SLUG, caller, URL, HEAD, Who(name=AUTHOR, swarm=SLUG, task="t1")))
    if not stop:
        expected.append(call.freshness(URL, HEAD))
    assert events.mock_calls == expected
