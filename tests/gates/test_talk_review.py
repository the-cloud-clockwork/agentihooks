from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.gates import talk


@pytest.mark.feature("reviewer-worker-gates")
@pytest.mark.parametrize("lane", ["eng", "ci", "review"])
def test_worker_lanes_cannot_talk_past_the_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lane: str) -> None:
    name = "reviewer@100001-0001"
    monkeypatch.setattr("scripts.swarm.naming.lane_of", lambda by: lane)
    monkeypatch.setattr("scripts.swarm.naming.resolve_name", lambda by: by)
    mark = SimpleNamespace(outcome_at=1, talk=talk.BUDGET)
    monkeypatch.setattr(talk.Budget, "_marks", lambda self: ("enforce", SimpleNamespace(read=lambda by: mark)))
    monkeypatch.setattr(talk.Budget, "_exempt", lambda self, doc, by, ctx: False)
    ctx = SimpleNamespace(at=1000, refused=[])
    apply = Mock(return_value=True)
    op = {"op": "add", "thread": "chat", "by": name, "text": "update"}
    assert not talk.Budget("sw", home=tmp_path).apply({"tasks": []}, op, ctx, apply)
    assert ctx.refused == [talk.refusal(name, talk.BUDGET, "sw")]
    apply.assert_not_called()
