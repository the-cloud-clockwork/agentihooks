from argparse import Namespace
from unittest.mock import Mock, call

import pytest

from scripts.gates.base import Who
from scripts.review import gate, record
from scripts.swarm.store import AgentRecord
from tests.review.fakes import AUTHOR, HEAD, REVIEWER, SLUG, URL, FakeForge, swarm

pytestmark = pytest.mark.unit


@pytest.mark.feature("review-findings-queue-gate")
def test_queue_admission_preserves_intent_review_and_freshness_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    found = swarm({"review": "enforce"})
    forge = FakeForge()
    record.post(found, SLUG, forge, URL, REVIEWER, "r1", ["First"])
    record.mark(found, SLUG, forge, URL, AUTHOR, [], "")
    caller = AgentRecord(name=AUTHOR, lane="eng", task="t1")
    args = Namespace(slug=SLUG, url=URL)
    events = Mock()
    intent = Mock()
    review = Mock(wraps=gate.admit)
    freshness = Mock(return_value=None)
    events.attach_mock(intent, "intent")
    events.attach_mock(review, "review")
    events.attach_mock(freshness, "freshness")
    monkeypatch.setattr(gate, "admit", review)
    monkeypatch.setattr(gate.queue_exit, "require_fresh", freshness)
    assert gate.queue_admit(found, args, caller, caller, HEAD, intent, None) is None
    assert events.mock_calls == [
        call.intent(found, args, caller, HEAD),
        call.review(found, SLUG, caller, URL, HEAD, Who(name=AUTHOR, swarm=SLUG, task="t1")),
        call.freshness(URL, HEAD),
    ]
