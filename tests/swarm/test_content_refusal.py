from pathlib import Path

import pytest

from scripts.swarm.delivery import HerdrMessenger
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import AgentRecord


CAPTURE = Path("tests/fixtures/swarm/codex-content-refusal.txt").read_text()


@pytest.mark.parametrize("component", [HerdrRuntime, HerdrMessenger])
def test_completed_codex_refusal_is_idle_for_the_normal_inbox_wake(component, tmp_path):
    agent = AgentRecord("worker", "eng", "t1", pane_id="p1", harness="codex")

    def herdr(args):
        if args[1] == "get":
            return {"name": "worker", "pane_id": "p1", "agent_status": "blocked"}
        return {"text": CAPTURE}

    runtime = component(herdr=herdr)
    status = runtime.status(agent) if component is HerdrRuntime else runtime.agent_status(agent)
    assert status == "idle"
