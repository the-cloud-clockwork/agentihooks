"""The feature suite harness: a scripted agent on real Redis, Postgres and a ledger server keeps the launch contract."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts.swarm import idle, launch_check
from tests.features.conftest import TASK, Agent

CANARY_DELAY_S = 2.0
WAIT_S = 60.0
HANDOFF = """# Handoff v2
## Intent
Prove the feature suite harness on real services.
## Done
- Canary reported and heartbeat seen, `pytest tests/features` launch check passed
## Stopped at
The harness test asked for this handoff.
## Decisions and promises
None
## Next
1. Continue the scripted agent task; done when its test passes.
## Read first
- ledger:{slug}/tasks/{task} the task and its contract
<!-- handoff complete -->
"""


def _until(check: Callable[[], object], agent: Agent, what: str) -> object:
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        if found := check():
            return found
        if agent.process.poll() is not None:
            break
        time.sleep(0.2)
    pytest.fail(f"{what} did not happen; scripted agent exit {agent.process.poll()}:\n{agent.log.read_text()}")


@pytest.mark.feature("feature-suite-harness")
def test_a_scripted_agent_reports_its_canary_beats_and_hands_off_through_the_ledger_server(
    scripted_agent_launch: Callable[..., Agent], tmp_path: Path
) -> None:
    agent = scripted_agent_launch(canary_delay_s=CANARY_DELAY_S)
    store, slug = agent.swarm.store, agent.swarm.ledger.slug
    record = agent.record
    assert launch_check.canary(store, slug, record)[1] == {"canary": {"expected": "validated", "actual": "pending"}}

    def confirmed() -> object:
        current, found = launch_check.canary(store, slug, record)
        return current if not found and current is not record else None

    bound = _until(confirmed, agent, "the canary report")
    validation = bound.profile_decision["validation"]
    assert validation["state"] == "validated"
    assert validation["pid"] == agent.process.pid
    assert validation["profile"] == record.profile and validation["harness"] == record.harness
    assert bound.launch_timings["harness_at"] >= record.started_at

    beat = _until(lambda: idle.heartbeat(store.redis, slug, record.name), agent, "a heartbeat")
    assert beat["state"] == idle.WORKING

    doc = tmp_path / "handoff.md"
    doc.write_text(HANDOFF.format(slug=slug, task=TASK))
    result = agent.hand_off(doc)
    assert result["returncode"] == 0, result
    assert store.handoff(slug, TASK) == doc.read_text()
    assert [a.state for a in store.agents(slug) if a.name == record.name] == ["finished"]
    assert agent.process.wait(timeout=WAIT_S) == 0
