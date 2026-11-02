import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

ENV = {"AGENTIHOOKS_SWARM": "grade", "AGENTIHOOKS_SWARM_TASK": "work", "AGENTIHOOKS_AGENT_NAME": "engineer"}


class Ledger:
    """A ledger fake that applies brain operations to one task document."""

    def __init__(self, task: dict[str, Any]) -> None:
        self.doc: dict[str, Any] = {"tasks": [task], "_meta": {}}
        self.reads = 0
        self.refused: list[str] = []

    def task(self, slug: str, task_id: str) -> dict[str, Any]:
        self.reads += 1
        return next(row for row in self.doc["tasks"] if row["id"] == task_id)

    def record_learning(self, slug: str, ops: list[dict[str, Any]]) -> None:
        from scripts.swarm_ledger import ledger_brain

        ctx = SimpleNamespace(at=100000, meta=self.doc["_meta"], dirty=False, refused=self.refused, record=print)
        for op in ops:
            ledger_brain.check(op)
            assert ledger_brain.apply(self.doc, op, ctx), self.refused


def task() -> dict[str, Any]:
    fixture = Path(__file__).parents[1] / "fixtures/swarm_v2/learning-outcomes.json"
    return json.loads(fixture.read_text())["task"]


def transcript(tmp_path: Path, *texts: str) -> str:
    path = tmp_path / "session.jsonl"
    path.write_text("".join(json.dumps({"type": "assistant", "message": text}) + "\n" for text in texts))
    return str(path)


def test_lesson_and_decision_markers_become_two_learning_outcomes(tmp_path: Path) -> None:
    from hooks.context import task_learning

    ledger = Ledger(task())
    path = transcript(
        tmp_path,
        "Fixed.\n<!-- @lesson -->\nPin the probe before the rollout.\n<!-- @/lesson -->",
        "<!-- @decision date=2026-11-02 -->\nKeep the boundary in code.\n<!-- @/decision -->\n"
        "<!-- @milestone status=done -->\nShipped.\n<!-- @/milestone -->",
    )
    assert task_learning.record("session-1", path, ENV, ledger, lambda name: "eng-1@grade") == 2
    outcomes = ledger.doc["tasks"][0]["learning_outcomes"]
    assert [(row["item"], row["by"], row["learning"]["kind"], row["learning"]["seat"]) for row in outcomes] == [
        ("tasks/work", "engineer", "lesson", "eng-1@grade"),
        ("tasks/work", "engineer", "decision", "eng-1@grade"),
    ]
    assert outcomes[0]["learning"]["content"] == "Pin the probe before the rollout."
    assert outcomes[1]["learning"]["sources"] == ["session:session-1"]
    from scripts.swarm_ledger import ledger_brain

    assert ledger_brain.completion(ledger.doc["tasks"][0]) is None


def test_a_session_without_markers_records_nothing(tmp_path: Path) -> None:
    from hooks.context import task_learning

    ledger = Ledger(task())
    path = transcript(tmp_path, "Nothing learned.", "<!-- @milestone -->\nShipped.\n<!-- @/milestone -->")
    assert task_learning.record("session-1", path, ENV, ledger, lambda name: "eng-1@grade") == 0
    assert "learning_outcomes" not in ledger.doc["tasks"][0]
    assert ledger.reads == 0


def test_a_repeated_stop_records_each_marker_once(tmp_path: Path) -> None:
    from hooks.context import task_learning

    ledger = Ledger(task())
    path = transcript(tmp_path, "<!-- @lesson -->\nOne lesson.\n<!-- @/lesson -->")
    for _ in range(2):
        assert task_learning.record("session-1", path, ENV, ledger, lambda name: "eng-1@grade") == 1
    assert len(ledger.doc["tasks"][0]["learning_outcomes"]) == 1


@pytest.mark.parametrize("missing", list(ENV))
def test_a_session_outside_a_swarm_task_records_nothing(tmp_path: Path, missing: str) -> None:
    from hooks.context import task_learning

    ledger = Ledger(task())
    path = transcript(tmp_path, "<!-- @lesson -->\nOne lesson.\n<!-- @/lesson -->")
    env = {key: value for key, value in ENV.items() if key != missing}
    assert task_learning.record("session-1", path, env, ledger, lambda name: "eng-1@grade") == 0
    assert ledger.reads == 0


@pytest.mark.parametrize("field", ["proof", "brain_policy"])
def test_a_task_without_proof_or_brain_policy_records_nothing(tmp_path: Path, field: str) -> None:
    from hooks.context import task_learning

    row = task()
    del row[field]
    ledger = Ledger(row)
    path = transcript(tmp_path, "<!-- @lesson -->\nOne lesson.\n<!-- @/lesson -->")
    assert task_learning.record("session-1", path, ENV, ledger, lambda name: "eng-1@grade") == 0
    assert "learning_outcomes" not in ledger.doc["tasks"][0]


def test_learning_accepts_a_bounded_seat_only() -> None:
    from scripts.swarm_ledger import ledger_brain

    learning = {"kind": "lesson", "content": "A lesson", "sources": ["session:s"]}
    ledger_brain.check_learning({**learning, "seat": "eng-1@grade"})
    for seat in ("", "x" * 201, 7):
        with pytest.raises(ValueError):
            ledger_brain.check_learning({**learning, "seat": seat})
