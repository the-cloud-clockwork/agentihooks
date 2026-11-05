"""A handed off master that only another hive lists is retired through that hive, so its successor launches."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import terminate_agent
from scripts.hive.orders import CommandOrder
from scripts.swarm import commands
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

    def __init__(self, home: Path, redis: object) -> None:
        super().__init__()
        self.orders: list[tuple[str, str, str]] = []
        self.herdr = HerdrRuntime(home=home)
        self.herdr.end = self.end
        self.herdr._order = self.order
        self.herdr.hive = SimpleNamespace(holder=self.holder, redis=redis)

    def holder(self, name: str) -> str:
        return WORKSTATION if name in self.live else ""

    def end(self, name: str, pid: int | None, homes: list[str], start: int = 0) -> Outcome:
        return Outcome(refusal=NO_PROCESS)

    def order(self, host: str, order: CommandOrder, name: str) -> bool:
        self.orders.append((host, order.action, order.name))
        self.live.discard(order.name)
        return True

    def retire(self, agent: AgentRecord, homes: tuple[str, ...] = ()) -> bool:
        return self.herdr.retire(agent, homes)


@pytest.fixture
def controller(store: RedisStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ControllerRuntime:
    monkeypatch.setattr(terminate_agent, "sessions", lambda: [])
    runtime = ControllerRuntime(tmp_path, store.redis)
    runtime.store = store
    return runtime


def test_controller_retires_a_handed_off_workstation_master_and_launches_its_successor(
    store: RedisStore, controller: ControllerRuntime
) -> None:
    tick("sw", store, tasks(), controller, 1)
    (boss,) = masters(store)
    store.put_agent("sw", replace(boss, state="finished"))
    tick("sw", store, tasks(), controller, 7)
    tick("sw", store, tasks(), controller, 8)
    assert controller.orders == [(WORKSTATION, "reap", boss.name)]
    assert [a.name for a in masters(store)] == ["master@a1b2c3-0002"]
    assert controller.live == {"master@a1b2c3-0002"}


def test_a_master_no_hive_lists_keeps_the_local_retire(store: RedisStore, controller: ControllerRuntime) -> None:
    tick("sw", store, tasks(), controller, 1)
    (boss,) = masters(store)
    controller.live.discard(boss.name)
    assert controller.retire(boss) is False
    assert controller.orders == []
    assert controller.herdr.refusal(boss) == {"process": 0, "refusal": NO_PROCESS}


def test_a_name_the_own_hive_lists_keeps_the_local_retire(
    store: RedisStore, controller: ControllerRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    tick("sw", store, tasks(), controller, 1)
    (boss,) = masters(store)
    monkeypatch.setattr(commands, "hive_id", lambda: WORKSTATION)
    assert controller.retire(boss) is False
    assert controller.orders == []


def test_a_name_a_local_session_holds_keeps_the_local_retire(
    store: RedisStore, controller: ControllerRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    tick("sw", store, tasks(), controller, 1)
    (boss,) = masters(store)
    held = SimpleNamespace(name=boss.name, process=SimpleNamespace(pid=7))
    monkeypatch.setattr(terminate_agent, "sessions", lambda: [held])
    assert controller.retire(boss) is False
    assert controller.orders == []


def test_a_runtime_without_hive_reports_keeps_the_local_retire(
    store: RedisStore, controller: ControllerRuntime
) -> None:
    tick("sw", store, tasks(), controller, 1)
    (boss,) = masters(store)
    controller.herdr.hive = None
    assert controller.retire(boss) is False
    assert controller.orders == []
