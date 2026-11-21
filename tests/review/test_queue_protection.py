"""Protects the swarm merge queue path the review gate joins: a default swarm keeps queueing an unreviewed head."""

from collections.abc import Callable
from typing import Any

import pytest

from scripts.gates import log
from scripts.swarm import cli, merge_queue, queue_exit
from tests.review.fakes import AUTHOR, FEATURE, HEAD, SLUG, URL, swarm

pytestmark = pytest.mark.unit


@pytest.mark.feature(FEATURE)
def test_a_default_swarm_queues_a_head_nobody_reviewed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "connect", lambda: swarm({}))
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", AUTHOR)
    monkeypatch.setattr(cli, "_queue_intent", lambda store, args, agent, head: None)
    monkeypatch.setattr(queue_exit, "require_fresh", lambda url, head: None)
    monkeypatch.setattr(log, "append", lambda slug, row, home=None: None)
    admitted: list[str] = []

    def operate(action: str, url: str, admit: Callable[[str], Any], **kwargs: Any) -> dict[str, Any]:
        admit(HEAD)
        admitted.append(url)
        return {"queued": True}

    monkeypatch.setattr(merge_queue, "operate", operate)
    assert cli.main([SLUG, "merge", "queue", URL]) == 0
    assert admitted == [URL]
