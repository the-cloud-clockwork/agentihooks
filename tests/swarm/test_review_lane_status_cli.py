from typing import Any

import pytest

from scripts.swarm.store import AgentRecord
from tests.swarm.test_cli import env, run

__all__ = ["env"]
pytestmark = pytest.mark.feature("swarm-review-lane-reporting")


@pytest.mark.feature("swarm-review-lane-reporting")
def test_status_text_prints_a_published_reviewer_lane(env: tuple[Any, ...], capsys: pytest.CaptureFixture[str]) -> None:
    store, _, runtime = env
    run("sw", "create", "--repo", "/repo")
    name = "reviewer@a1b2c3-0001"
    store.put_agent("sw", AgentRecord(name, "review", "r1", profile="reviewer", harness="codex"))
    runtime.live.add(name)
    run("sw", "status")
    rows = [line.split("\t") for line in capsys.readouterr().out.splitlines() if line.startswith(name + "\t")]
    assert len(rows) == 1
    assert "review" in rows[0]
    assert "reviewer" in rows[0]
