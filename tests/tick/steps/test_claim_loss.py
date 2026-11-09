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
    monkeypatch.setenv("SWARM_HIVE_ID", "workstation")


@pytest.mark.feature("swarm-reopened-claim-loss")
@pytest.mark.parametrize("owner", ["", None, "another-worker"])
def test_reopened_or_reassigned_claims_end_the_previous_worker(owner: str | None) -> None:
    agent = AgentRecord(name="engineer", lane="eng", task="task", hive="swarm-hive-003")
    row = {"state": "open", "claimed_by": owner}
    assert common._current_owner(agent, row) is False
    assert common._ended(agent, {"task": row}) is True


@pytest.mark.feature("swarm-reopened-claim-loss")
@pytest.mark.parametrize("backend", ["local", "remote"])
@pytest.mark.parametrize("owner", ["", "another-worker"])
def test_tick_retires_a_lost_claim_before_restore_or_watch(
    monkeypatch: pytest.MonkeyPatch, backend: str, owner: str
) -> None:
    agent = AgentRecord(name="engineer", lane="eng", task="task", runtime_backend=backend, hive="swarm-hive-003")
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


@pytest.mark.feature("swarm-reopened-claim-loss")
@pytest.mark.parametrize("owner", ["", "successor", "engineer"])
@pytest.mark.parametrize("reopened", [False, True])
def test_retirement_preserves_other_claims_and_fences_its_own_reopen(
    monkeypatch: pytest.MonkeyPatch, owner: str, reopened: bool
) -> None:
    agent = AgentRecord(name="engineer", lane="eng", task="task", seat="eng-1@swarm", hive="swarm-hive-003")
    row = {"state": "claimed", "claimed_by": owner}
    rows = {"task": row}
    store, ledger, runtime = Mock(), Mock(), Mock()
    store.redis.hexists.return_value = False
    store.handoff.return_value = "retained handoff"
    ledger.update_task.return_value = (
        {"state": "open", "claimed_by": ""} if reopened else {"state": "claimed", "claimed_by": "successor"}
    )
    monkeypatch.setattr(tick.master_retire, "forget", Mock())
    monkeypatch.setattr(tick.reports, "Reports", Mock(return_value=Mock(removed=Mock(return_value=False))))
    monkeypatch.setattr(tick.retire_watch, "reported_gone", Mock(return_value=False))
    monkeypatch.setattr(tick, "_refund_parked", Mock())
    monkeypatch.setattr(tick.reaper, "scratch_homes", Mock(return_value=[]))
    settle = Mock()
    monkeypatch.setattr(tick.exits, "settle", settle)
    monkeypatch.setattr(tick, "InboxStore", Mock(return_value="inbox"))

    assert agents._retire_ended("swarm", store, ledger, runtime, rows, agent, 1000) == ["retired engineer"]
    runtime.retire.assert_called_once_with(agent, homes=[])
    store.release.assert_called_once_with("swarm", "task", agent.name)
    store.drop_agent.assert_called_once_with("swarm", agent.name)
    if owner == agent.name:
        ledger.update_task.assert_called_once_with(
            "swarm",
            "task",
            {"state": "open", "claimed_by": ""},
            if_state=tick.ACTIVE,
        )
    else:
        ledger.update_task.assert_not_called()
        assert row["claimed_by"] == owner
    settle.assert_called_once_with(
        "inbox", agent.name, agent.seat if owner == agent.name and reopened else "", "exited"
    )
