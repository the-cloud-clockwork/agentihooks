from collections.abc import Callable
from typing import Any
from unittest.mock import Mock

import pytest

from scripts.swarm import tick
from scripts.swarm.store import AgentRecord
from scripts.tick.steps import agents, common


def _timed(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return function(*args, **kwargs)


@pytest.fixture(autouse=True)
def timing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tick.timing, "timed", _timed)


@pytest.mark.feature("swarm-reopened-claim-loss")
@pytest.mark.parametrize("owner", ["", None, "another-worker"])
def test_reopened_or_reassigned_claims_end_the_previous_worker(owner: str | None) -> None:
    agent = AgentRecord(name="engineer", lane="eng", task="task")
    row = {"state": "open", "claimed_by": owner}
    assert common._current_owner(agent, row) is False
    assert common._ended(agent, {"task": row}) is True


@pytest.mark.feature("swarm-reopened-claim-loss")
@pytest.mark.parametrize("backend", ["local", "remote"])
@pytest.mark.parametrize("owner", ["", "another-worker"])
def test_tick_retires_a_lost_claim_before_restore_or_watch(
    monkeypatch: pytest.MonkeyPatch, backend: str, owner: str
) -> None:
    agent = AgentRecord(name="engineer", lane="eng", task="task", runtime_backend=backend)
    row = {"state": "open", "claimed_by": owner}
    retired, restored, watched = Mock(return_value=["retired"]), Mock(), Mock()
    monkeypatch.setattr(tick, "_acknowledging", Mock(return_value=False))
    monkeypatch.setattr(tick, "_remote_gone_reason", Mock(return_value=""))
    monkeypatch.setattr(tick, "_retire_ended", retired)
    monkeypatch.setattr(tick, "_restore_owner", restored)
    monkeypatch.setattr(tick, "_watch_agent", watched)
    store, ledger, runtime = Mock(), Mock(), Mock()
    rows = {"task": row}
    judging = agents._Reaping("swarm", store, ledger, runtime, rows, {agent.name}, 1000)

    assert agents._reap_agent(judging, agent) == ["retired"]
    retired.assert_called_once_with("swarm", store, ledger, runtime, rows, agent, 1000)
    restored.assert_not_called()
    watched.assert_not_called()


@pytest.mark.feature("swarm-reopened-claim-loss")
def test_a_live_workstation_worker_with_an_intact_claim_can_restore_its_state() -> None:
    agent = AgentRecord(name="engineer", lane="eng", task="task")
    row = {"state": "open", "claimed_by": agent.name}
    store, ledger = Mock(), Mock()
    ledger.update_task.return_value = {"state": "claimed", "claimed_by": agent.name}
    rows = {"task": row}

    agents._restore_owner("swarm", store, ledger, rows, agent)

    store.ensure_claim.assert_called_once_with("swarm", agent, tick.LEASE_MS)
    ledger.update_task.assert_called_once_with(
        "swarm",
        "task",
        {"claimed_by": agent.name, "state": "claimed"},
        if_state=("open",),
        if_match={"claimed_by": agent.name, "claim_generation": 0, "awaiting": ""},
    )
