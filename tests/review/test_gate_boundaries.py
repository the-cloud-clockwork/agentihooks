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


@pytest.mark.feature("review-queue-gate")
@pytest.mark.parametrize("claimed", [AUTHOR, None])
@pytest.mark.parametrize(
    "url,context,expected",
    [
        (URL, {"task": "t7"}, "t7"),
        (URL, None, "t1"),
        ("https://github.com/o/r/pull/8", None, ""),
    ],
)
def test_the_author_is_the_claimant_of_the_routed_task_or_the_only_targeting_task(
    monkeypatch: pytest.MonkeyPatch, claimed: str | None, url: str, context: dict[str, str] | None, expected: str
) -> None:
    from scripts.swarm.ledger_client import LedgerClient

    gate = import_module("scripts.review.gate")

    def task(client: LedgerClient, slug: str, task_id: str) -> dict[str, str | None] | None:
        return {"id": task_id, "claimed_by": claimed} if (slug, task_id) == (SLUG, "t7") else None

    def tasks_targeting(client: LedgerClient, slug: str, target: str) -> list[dict[str, str | None]]:
        return [{"id": "t1", "claimed_by": claimed}] if (slug, target) == (SLUG, URL) else []

    monkeypatch.setattr(LedgerClient, "task", task)
    monkeypatch.setattr(LedgerClient, "tasks_targeting", tasks_targeting)
    review, freshness = Mock(return_value=None), Mock(return_value=None)
    monkeypatch.setattr(gate, "admit", review)
    monkeypatch.setattr(gate.queue_exit, "require_fresh", freshness)
    found, args = store(), argparse.Namespace(slug=SLUG, url=url)
    caller = AgentRecord("master@a1b2c3-0002", "master", "master")
    assert gate.queue_admit(found, args, None, caller, HEAD, Mock(), context) is None
    author = Who(name=claimed or "", swarm=SLUG, task=expected) if expected else Who(swarm=SLUG)
    assert review.mock_calls == [call(found, SLUG, caller, url, HEAD, author)]
    assert freshness.mock_calls == [call(url, HEAD)]
