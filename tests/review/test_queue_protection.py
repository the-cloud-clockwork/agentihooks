from typing import Any

import pytest

from scripts.swarm import cli, merge_queue, queue_exit
from scripts.swarm.store import SwarmError
from tests.swarm.test_merge_queue import URL, swarm_of


@pytest.mark.feature("review-queue-gate")
def test_intent_refusal_stops_freshness_and_queue_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "connect", lambda: swarm_of("eng"))
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    monkeypatch.setattr(cli.done_gate, "require_local", lambda *args: None)
    monkeypatch.setattr(cli, "_require_target", lambda *args: None)
    queued: list[str] = []

    def intent(*args: Any) -> None:
        raise SwarmError("intent refused")

    def operate(action: str, url: str, **kwargs: Any) -> dict[str, Any]:
        kwargs["admit"]("observed-head")
        queued.append(url)
        return {}

    monkeypatch.setattr(cli, "_queue_intent", intent)
    monkeypatch.setattr(queue_exit, "require_fresh", lambda *args: pytest.fail("intent must run first"))
    monkeypatch.setattr(merge_queue, "operate", operate)
    assert cli.main(["sw", "merge", "queue", URL]) == 1
    assert queued == []


@pytest.mark.feature("review-queue-gate")
@pytest.mark.parametrize("action", ["state", "dequeue"])
def test_nonqueue_operations_do_not_run_admission(monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    monkeypatch.setattr(cli, "connect", lambda: swarm_of("eng"))
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    monkeypatch.setattr(cli.done_gate, "require_local", lambda *args: None)
    monkeypatch.setattr(cli, "_require_target", lambda *args: None)
    monkeypatch.setattr(cli, "_queue_intent", lambda *args: pytest.fail("nonqueue intent"))
    monkeypatch.setattr(queue_exit, "require_fresh", lambda *args: pytest.fail("nonqueue freshness"))

    def operate(verb: str, url: str, **kwargs: Any) -> dict[str, Any]:
        assert verb == action
        assert "admit" not in kwargs
        return {}

    monkeypatch.setattr(merge_queue, "operate", operate)
    assert cli.main(["sw", "merge", action, URL]) == 0
