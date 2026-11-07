"""A handed off master that only another hive lists is retired through that hive, so its successor launches."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import terminate_agent
from scripts.hive.orders import CommandOrder
from scripts.swarm import commands, retire_watch
from scripts.swarm.reaper import Outcome
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import AgentRecord, RedisStore
from scripts.swarm.tick import tick
from tests.swarm import test_tick
from tests.swarm.test_master_affinity import AffinityRuntime
from tests.swarm.test_tick import masters, tasks

store = test_tick.store

WORKSTATION = "workstation-hive"
NO_PROCESS = "no such process in the controller pod"


class ControllerRuntime(AffinityRuntime):
    """The controller pod: no local sessions, while the workstation hive reports the master it holds."""

    def __init__(self, home: Path, store: RedisStore) -> None:
        super().__init__()
        self.store: Any = store
        self.orders: list[tuple[str, str, str]] = []
        self.delegated: list[str] = []
        self.herdr: Any = HerdrRuntime(home=home)
        self.reap = self.herdr.reap_name
        self.herdr.reap_name = self.reap_name
        self.herdr.end = self.end
        self.herdr._order = self.order
        self.herdr.hive = SimpleNamespace(holder=self.holder, executions_of=lambda hive, name: [], redis=store.redis)

    def holder(self, name: str) -> str:
        return WORKSTATION if name in self.live else ""

    def reap_name(self, name: str) -> bool:
        self.delegated.append(name)
        return self.reap(name)

    def end(self, name: str, pid: int | None, homes: list[str], start: int = 0) -> Outcome:
        return Outcome(refusal=NO_PROCESS)

    def order(self, host: str, order: CommandOrder, name: str) -> bool:
        self.orders.append((host, order.action, order.name))
        self.live.discard(order.name)
        return True

    def retire(self, agent: AgentRecord, homes: tuple[str, ...] = ()) -> bool:
        return self.herdr.retire(agent, homes)

    def refusal(self, agent: AgentRecord) -> dict:
        return self.herdr.refusal(agent)


@pytest.fixture
def controller(store: RedisStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ControllerRuntime:
    monkeypatch.setattr(terminate_agent, "sessions", lambda: [])
    return ControllerRuntime(tmp_path, store)


def test_controller_retires_a_handed_off_workstation_master_and_launches_its_successor(
    store: RedisStore, controller: ControllerRuntime
) -> None:
    tick("sw", store, tasks(), controller, 1)
    (boss,) = masters(store)
    store.put_agent("sw", replace(boss, state="finished"))
    tick("sw", store, tasks(), controller, 7)
    tick("sw", store, tasks(), controller, 8)
    assert controller.orders == [(WORKSTATION, "reap", boss.name)]
    assert controller.delegated == [boss.name]
    assert [a.name for a in masters(store)] == ["master@a1b2c3-0002"]
    assert controller.live == {"master@a1b2c3-0002"}


def test_an_unacknowledged_hive_reap_leaves_a_retire_failure_naming_the_hive(
    store: RedisStore, controller: ControllerRuntime
) -> None:
    del controller.herdr._order
    tick("sw", store, tasks(), controller, 1)
    (boss,) = masters(store)
    store.put_agent("sw", replace(boss, state="finished"))
    tick("sw", store, tasks(), controller, 7)
    (row,) = retire_watch.rows(store, "sw")
    assert (row["agent"], row["process"]) == (boss.name, 0)
    assert row["refusal"] == f"reap ordered on hive {WORKSTATION}, waiting for its acknowledgement"
    assert [a.name for a in masters(store)] == [boss.name]


def test_a_master_no_hive_lists_keeps_the_local_retire(store: RedisStore, controller: ControllerRuntime) -> None:
    tick("sw", store, tasks(), controller, 1)
    (boss,) = masters(store)
    controller.live.discard(boss.name)
    assert controller.retire(boss) is False
    assert controller.delegated == []
    assert controller.herdr.refusal(boss) == {"process": 0, "refusal": NO_PROCESS}


def test_a_name_the_own_hive_lists_keeps_the_local_retire(
    store: RedisStore, controller: ControllerRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    tick("sw", store, tasks(), controller, 1)
    (boss,) = masters(store)
    monkeypatch.setattr(commands, "hive_id", lambda: WORKSTATION)
    assert controller.retire(boss) is False
    assert controller.delegated == []


def test_a_reap_on_the_hive_that_lists_the_name_ends_it_locally(
    controller: ControllerRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    ended: list[list[int]] = []
    controller.live.add("master@sw-gone")
    controller.herdr.reap = lambda pids: ended.append(pids) or Outcome()
    monkeypatch.setattr(commands, "hive_id", lambda: WORKSTATION)
    assert controller.reap("master@sw-gone") is True
    assert (ended, controller.orders) == ([[]], [])


def test_a_name_a_local_session_holds_keeps_the_local_retire(
    store: RedisStore, controller: ControllerRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    tick("sw", store, tasks(), controller, 1)
    (boss,) = masters(store)
    held = SimpleNamespace(name=boss.name, process=SimpleNamespace(pid=7))
    monkeypatch.setattr(terminate_agent, "sessions", lambda: [held])
    assert controller.retire(boss) is False
    assert controller.delegated == []


def test_a_runtime_without_hive_reports_keeps_the_local_retire(
    store: RedisStore, controller: ControllerRuntime
) -> None:
    tick("sw", store, tasks(), controller, 1)
    (boss,) = masters(store)
    controller.herdr.hive = None
    assert controller.retire(boss) is False
    assert controller.delegated == []
