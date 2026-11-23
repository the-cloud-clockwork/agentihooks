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


@pytest.mark.feature("review-queue-gate")
@pytest.mark.parametrize("targets", [[], [{"id": "t1"}, {"id": "t2"}]])
def test_a_master_without_one_target_never_substitutes_itself_for_the_author(
    monkeypatch: pytest.MonkeyPatch, targets: list[dict[str, str]]
) -> None:
    from types import SimpleNamespace

    from scripts.gates import log
    from scripts.swarm.ledger_client import LedgerClient

    gate = import_module("scripts.review.gate")
    metadata = store()
    monkeypatch.setattr(metadata, "config", lambda slug: SimpleNamespace(gates={"review": "enforce"}))
    monkeypatch.setattr(LedgerClient, "tasks_targeting", lambda *args: targets)
    monkeypatch.setattr(gate.record, "read", lambda *args: None)
    monkeypatch.setattr(log, "append", lambda *args: None)
    monkeypatch.setattr(gate.queue_exit, "require_fresh", lambda *args: pytest.fail("missing author admitted"))
    caller = AgentRecord("master@a1b2c3-0002", "master", "master")
    with pytest.raises(SwarmError, match="no author recorded"):
        gate.queue_admit(metadata, argparse.Namespace(slug=SLUG, url=URL), None, caller, HEAD, Mock(), None)
