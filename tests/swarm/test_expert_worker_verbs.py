"""An expert claims no task: the swarm's worker verbs refuse it, and it still queues its own pull requests."""

import json
from typing import Any

import pytest

from scripts.swarm import cli
from scripts.swarm.store import AgentRecord, RedisStore
from tests.swarm.test_worker_verb_neighbours import PULL, ran, store

pytestmark = pytest.mark.unit
_fixtures = (store,)

EXPERT = "expert@a1b2c3-0001"
REFUSED = f"swarm: {EXPERT} is an expert; an expert works its mission and claims no task\n"


@pytest.fixture
def expert(store: RedisStore) -> RedisStore:
    store.put_agent("sw", AgentRecord(EXPERT, "expert", "expert-1", seat="expert-1@sw", state="working"))
    return store


@pytest.mark.feature("expert-claims-nothing")
@pytest.mark.parametrize(
    "argv",
    [
        ("issue", "https://github.com/o/r/issues/1"),
        ("review",),
        ("branch",),
        ("publish", "-m", "one more step"),
        ("pr", PULL),
        ("done", "--pr", PULL),
        ("block", "stuck on a decision"),
        ("trace-plan",),
        ("progress", "--doing", "the next seam", "--ends-when", "its test is green"),
        ("park", "handoff.md"),
        ("ready", "handoff.md"),
        ("restack",),
    ],
)
def test_an_expert_runs_no_worker_verb(
    expert: RedisStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: tuple[str, ...]
) -> None:
    assert ran(monkeypatch, EXPERT, *argv) == 1
    assert capsys.readouterr().err == REFUSED
    assert [agent.state for agent in expert.agents("sw") if agent.name == EXPERT] == ["working"]


@pytest.mark.feature("expert-claims-nothing")
@pytest.mark.parametrize("action,admitted", [("queue", ["fresh head-1"]), ("dequeue", [])])
def test_an_expert_queues_and_dequeues_its_pull_request_with_no_task_gate(
    expert: RedisStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    action: str,
    admitted: list[str],
) -> None:
    seen: list[dict[str, Any]] = []

    def operate(asked: str, url: str, **options: Any) -> dict[str, str]:
        admit = options.get("admit")
        seen.append(
            {
                "action": asked,
                "url": url,
                "context": options["journal"].context,
                "admitted": [admit("head-1")] if admit else [],
            }
        )
        return {"action": asked, "url": url}

    def task_gate(*args: Any, **options: Any) -> None:
        raise AssertionError("an expert holds no task for a task gate to judge")

    monkeypatch.setattr(cli.merge_queue, "operate", operate)
    monkeypatch.setattr(cli.done_gate, "require_local", task_gate)
    monkeypatch.setattr(cli, "_require_target", task_gate)
    monkeypatch.setattr(cli, "_queue_intent", task_gate)
    monkeypatch.setattr(cli.queue_exit, "require_fresh", lambda url, head: f"fresh {head}" if url == PULL else "")
    assert ran(monkeypatch, EXPERT, "merge", action, PULL) == 0
    assert json.loads(capsys.readouterr().out) == {"action": action, "url": PULL}
    assert seen == [{"action": action, "url": PULL, "context": {}, "admitted": admitted}]
