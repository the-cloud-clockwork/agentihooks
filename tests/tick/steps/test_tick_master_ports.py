"""The tick master step writes only through the ports it declares: the master launch lock through its coordination
port, and the outage pass's store writes, inbox items and ledger notices through the coordination, inbox and ledger
ports."""

import json
from typing import Any

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import master_lock, tick_master
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.tick.adapters.calls import SpawnStoreCalls
from scripts.tick.adapters.coordination import StoreCoordination
from scripts.tick.adapters.fakes import FakeCoordination, FakeInbox, FakeLedger
from scripts.tick.adapters.inbox import StoreInbox
from scripts.tick.deadline import Deadline
from scripts.tick.steps import CallStep, actions_of, masters
from tests.swarm.test_tick import FakeRuntime, store  # noqa: F401

pytestmark = pytest.mark.unit

DEADLINE = Deadline(10, lambda: 0)
NOW = 1_791_380_000_000
ENGINEER = "engineer@a1b2c3-0001"
HELD = "the master launch lock of swarm sw is held by tick master, so no second master starts"


class _Live:
    """A runtime whose live agents are the names it was given."""

    def __init__(self, *names: str) -> None:
        """Keep the live agent names."""
        self.names = set(names)

    def live_names(self) -> set[str]:
        """Return the live agent names."""
        return self.names


def _operations(port: FakeCoordination, kind: str) -> list[tuple[object, ...]]:
    """Return the arguments of each call of kind ``kind`` the fake coordination port recorded."""
    return [call.args for call in port.calls if call.operation == kind]


@pytest.mark.feature("master-launch-lock")
def test_the_coordination_port_takes_and_releases_the_master_launch_lock(store: RedisStore) -> None:  # noqa: F811
    port = StoreCoordination(store, "sw", "tick")
    token, refused = port.take_master_lock("tick master", deadline=DEADLINE)
    assert (token.startswith("tick master "), refused) == (True, "")
    assert store.redis.get(master_lock.key(store, "sw")) == token
    assert port.take_master_lock("master up", deadline=DEADLINE) == ("", HELD)
    port.release_master_lock("master up 1", deadline=DEADLINE)
    assert store.redis.get(master_lock.key(store, "sw")) == token
    port.release_master_lock(token, deadline=DEADLINE)
    assert store.redis.get(master_lock.key(store, "sw")) is None


@pytest.mark.feature("master-launch-lock")
def test_a_master_start_refused_through_the_coordination_port_launches_nothing(store: RedisStore) -> None:  # noqa: F811
    port = FakeCoordination(answers={"take_master_lock": ("", HELD)})
    runtime = FakeRuntime()
    said = masters._master("sw", store.config("sw"), store, runtime, NOW, coordination=port)
    assert said == [HELD]
    assert [call.operation for call in port.calls] == ["take_master_lock"]
    assert (runtime.masters, store.agents("sw")) == ([], [])


@pytest.mark.feature("master-launch-lock")
def test_the_master_launch_lock_taken_through_the_port_is_released_through_it(
    store: RedisStore,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = FakeCoordination(answers={"take_master_lock": ("tick master t1", "")})
    monkeypatch.setattr(masters, "_launch_master", lambda *args: ["spawned master m1"])
    assert masters._master("sw", store.config("sw"), store, FakeRuntime(), NOW, coordination=port) == [
        "spawned master m1"
    ]

    def failed(*args: Any) -> list[str]:
        raise RuntimeError("spawn failed")

    monkeypatch.setattr(masters, "_launch_master", failed)
    with pytest.raises(RuntimeError):
        masters._master("sw", store.config("sw"), store, FakeRuntime(), NOW, coordination=port)
    taken, released = _operations(port, "take_master_lock"), _operations(port, "release_master_lock")
    assert (taken, released) == ([("tick master",)] * 2, [("tick master t1",)] * 2)


@pytest.mark.feature("tick-master-effect-ports")
def test_the_inbox_port_sends_as_the_named_sender_with_its_fyi_mark(store: RedisStore) -> None:  # noqa: F811
    inbox = InboxStore(store.redis)
    port = StoreInbox(inbox, "tick@sw")
    port.send(ENGINEER, "the master is back", sender="swarm", fyi=True, deadline=DEADLINE)
    port.send(ENGINEER, "keep working", deadline=DEADLINE)
    assert [(item.sender, item.text, item.fyi) for item in inbox.inbox(ENGINEER)] == [
        ("swarm", "the master is back", True),
        ("tick@sw", "keep working", False),
    ]


@pytest.mark.feature("tick-master-effect-ports")
def test_the_outage_pass_saves_its_first_observation_through_the_coordination_port() -> None:
    port = FakeCoordination(answers={"store:config": SwarmConfig("sw", "/repo"), "store:agents": []})
    inbox, ledger = FakeInbox(), FakeLedger()
    calls = SpawnStoreCalls(port, DEADLINE, inbox)
    ports = tick_master.Ports(calls, inbox, ledger, DEADLINE)
    assert tick_master.run("sw", ports, _Live(), NOW, lambda: ["launched"], {}) == ["launched"]
    state = json.dumps({"since": NOW})
    assert _operations(port, "store_write") == [("redis.set", (calls.key("sw", tick_master.KEY), state), {})]
    assert (inbox.calls, ledger.calls) == ([], [])


@pytest.mark.feature("tick-master-effect-ports")
def test_the_outage_pass_promotes_through_the_coordination_inbox_and_ledger_ports(store: RedisStore) -> None:  # noqa: F811
    engineer = AgentRecord(ENGINEER, "eng", "t1", started_at=1, seat="eng-1@sw")
    store.put_agent("sw", engineer)
    tick_master._save(store, "sw", {"since": NOW, "forced_at": NOW, "failure": "spawn failed"})
    inbox, ledger = FakeInbox(), FakeLedger()
    ports = tick_master.Ports(
        SpawnStoreCalls(StoreCoordination(store, "sw", "tick"), DEADLINE, inbox), inbox, ledger, DEADLINE
    )
    actions = tick_master.run("sw", ports, _Live(ENGINEER), NOW + 1, lambda: [], {})
    assert actions == [
        f"promoted {ENGINEER} to restore the master: spawn failed",
        f"sent {ENGINEER} the promoted prompt",
    ]
    prompt = tick_master.prompt("sw", engineer, "spawn failed", 5, "spawn failed")
    assert [call.args for call in inbox.calls] == [(ENGINEER, prompt, "swarm", False)]
    notice = tick_master.PROMOTED_NOTICE.format(minutes="5")
    assert [(call.operation, call.args) for call in ledger.calls] == [("notify", (notice,))]
    assert tick_master.read(store, "sw")["promoted"] == ENGINEER


@pytest.mark.feature("tick-master-effect-ports")
def test_the_tick_master_step_declares_its_ports_and_writes_only_its_actions() -> None:
    step = next(step for step in masters.STEPS if step.name == "tick master")
    assert (step.effects, step.writes, step.lane) == (
        frozenset({"coordination", "inbox", "ledger"}),
        frozenset({actions_of("tick master")}),
        CallStep.lane,
    )
