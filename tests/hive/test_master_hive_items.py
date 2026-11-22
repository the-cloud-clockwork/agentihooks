"""A hive drain or a hive loss leaves one fyi item at the master seat of each swarm the hive served, once per change."""

import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

from scripts.hive import cli, registry, remove, sweep
from scripts.inbox.store import InboxStore, Item
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

pytestmark = pytest.mark.unit

NOW_MS = 1_800_000_000_000
QUIET_AT = NOW_MS - registry.QUIET_MS - 1
HIVE = "proof"
LOST_HIVE = "hive-a"
T0 = 1_000_000
SWARMS = ("sw", "other", "idle")
PLACED = (("sw", "eng", "working"), ("sw", "ci", "working"), ("other", "eng", "working"), ("idle", "eng", "finished"))


def _drained(reason: str, agents: str) -> str:
    return f"hive {HIVE} is draining: {reason}. Agents of this swarm on it: {agents}"


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> RedisStore:
    """Three swarms on one fake Redis, with a registry clock that moves one millisecond per read."""
    import fakeredis

    ticks = iter(range(NOW_MS, NOW_MS + 1_000_000))
    monkeypatch.setattr(registry, "wall_ms", lambda: next(ticks))
    result = RedisStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
    for slug in SWARMS:
        result.create(SwarmConfig(slug, "/repo", 1, 1))
    return result


def _agents(store: RedisStore) -> dict[str, list[str]]:
    """Two live agents of sw, one of other and a finished one of idle, all started on the hive; names per swarm."""
    names: dict[str, list[str]] = {}
    for slug, lane, state in PLACED:
        record = AgentRecord(store.next_name(slug, lane), lane, f"t-{lane}", seat=f"{lane}-1@{slug}", hive=HIVE)
        agent = store.start_execution(slug, replace(record, state="working"))
        if state != "working":
            store.put_agent(slug, replace(agent, state=state))
        names.setdefault(slug, []).append(agent.name)
    return {slug: sorted(found) for slug, found in names.items()}


def _master(store: RedisStore, slug: str) -> list[Item]:
    return InboxStore(store.redis).inbox(f"master@{slug}")


def _texts(store: RedisStore, slug: str) -> list[str]:
    return [item.text for item in _master(store, slug)]


class Clock:
    """A remove host clock whose first sleep finishes every agent of every swarm."""

    def __init__(self, store: RedisStore) -> None:
        self.store, self.now = store, 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        for slug in self.store.slugs():
            for agent in self.store.agents(slug):
                self.store.put_agent(slug, replace(agent, state="finished"))

    def __call__(self) -> float:
        return self.now


def _host(store: RedisStore, tmp_path: Path, namespace: str = "") -> remove.Host:
    ledger = SimpleNamespace(task=lambda *_: None, update_task=lambda *_, **__: None)
    clock = Clock(store)
    return remove.Host(
        sleep=clock.sleep,
        clock=clock,
        wall=lambda: NOW_MS / 1000,
        home=tmp_path,
        ledger=lambda: cast(remove.TaskLedger, ledger),
        namespace=lambda: namespace,
        worktrees=tmp_path / "worktrees",
    )


def _quiet(store: RedisStore) -> None:
    registry.update(store.redis, HIVE, ["ephemeral=yes", "ui=no"])
    registry.beat(store.redis, HIVE, {"heartbeat_at": QUIET_AT})
    store.redis.set(f"{registry.key(HIVE)}:sessions", "{}")


@pytest.mark.feature("master-hive-drain-item")
def test_a_sweep_drain_leaves_one_item_at_the_master_of_each_swarm_with_a_live_agent_on_the_hive(
    store: RedisStore,
) -> None:
    _quiet(store)
    names = _agents(store)

    assert sweep.sweep(store, NOW_MS) == [
        f"drained quiet ephemeral hive {HIVE}",
        f"kept quiet hive {HIVE} while 3 agent records still run on it",
    ]

    [item] = _master(store, "sw")
    assert (item.sender, item.fyi, item.text) == ("swarm", True, _drained(sweep.REASON, ", ".join(names["sw"])))
    assert _texts(store, "other") == [_drained(sweep.REASON, ", ".join(names["other"]))]
    assert _texts(store, "idle") == []


@pytest.mark.feature("master-hive-drain-item")
def test_a_second_sweep_of_the_draining_hive_sends_no_second_item(store: RedisStore) -> None:
    _quiet(store)
    _agents(store)
    sweep.sweep(store, NOW_MS)

    sweep.sweep(store, NOW_MS)

    assert [len(_master(store, slug)) for slug in SWARMS] == [1, 1, 0]


@pytest.mark.feature("master-hive-drain-item")
def test_hive_set_draining_tells_each_master_once_and_again_after_an_undrain(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "redis_client", lambda: store.redis)
    registry.update(store.redis, HIVE, ["ui=no"])
    names = _agents(store)
    sw, other = ", ".join(names["sw"]), ", ".join(names["other"])

    assert cli.main(["set", HIVE, "draining=yes"]) == 0
    assert cli.main(["set", HIVE, "draining=yes"]) == 0
    assert cli.main(["set", HIVE, "ui=no"]) == 0
    assert _texts(store, "sw") == [_drained("hive set", sw)]

    assert cli.main(["set", "--reason", "release roll", HIVE, "draining=no"]) == 0
    assert cli.main(["set", "--reason", "release roll", HIVE, "draining=yes"]) == 0

    assert _texts(store, "sw") == [_drained("hive set", sw), _drained("release roll", sw)]
    assert _texts(store, "other") == [_drained("hive set", other), _drained("release roll", other)]
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["draining"] == "yes"


@pytest.mark.feature("master-hive-drain-item")
def test_a_hive_remove_drain_tells_each_master_once_and_keeps_the_agent_orders(
    store: RedisStore, tmp_path: Path
) -> None:
    registry.update(store.redis, HIVE, ["ui=no"])
    names = _agents(store)
    plan = remove.Plan(HIVE, "anton-9", "p", wait_s=600)

    lines = remove.drain(store, plan, _host(store, tmp_path))
    remove.drain(store, plan, _host(store, tmp_path))

    assert lines[0].startswith(f"drained {HIVE}: 3 agents ordered to hand off; ")
    assert _texts(store, "sw") == [_drained("hive remove", ", ".join(names["sw"]))]
    assert _texts(store, "other") == [_drained("hive remove", ", ".join(names["other"]))]
    assert _texts(store, "idle") == []
    inbox = InboxStore(store.redis)
    for slug in ("sw", "other"):
        for name in names[slug]:
            assert [item.text for item in inbox.inbox(name)] == [remove.ORDER.format(hive=HIVE, slug=slug)]


@pytest.mark.feature("master-hive-drain-item")
def test_a_hive_stop_settle_tells_each_master_once_and_keeps_the_roll_orders(store: RedisStore, tmp_path: Path) -> None:
    registry.update(store.redis, HIVE, ["ui=no"])
    names = _agents(store)

    ended = remove.settle(store, HIVE, 600, _host(store, tmp_path))

    assert ended == f"hive {HIVE} drained: every agent handed off, 3 ordered to hand off"
    assert _texts(store, "sw") == [_drained("the hive is stopping", ", ".join(names["sw"]))]
    assert _texts(store, "other") == [_drained("the hive is stopping", ", ".join(names["other"]))]
    assert _texts(store, "idle") == []
    inbox = InboxStore(store.redis)
    for slug in ("sw", "other"):
        for name in names[slug]:
            assert [item.text for item in inbox.inbox(name)] == [remove.ROLL.format(hive=HIVE, slug=slug)]


@pytest.mark.feature("master-hive-drain-item")
def test_a_member_stop_while_another_member_serves_the_hive_sends_no_master_item(
    store: RedisStore, tmp_path: Path
) -> None:
    registry.update(store.redis, HIVE, ["ui=no"])
    registry.join(store.redis, HIVE, "old", NOW_MS)
    registry.join(store.redis, HIVE, "new", NOW_MS)
    _agents(store)

    ended = remove.settle(store, HIVE, 600, _host(store, tmp_path, "boot/old"), "old")

    assert ended.endswith("only member old drained, the hive stays open for its other members")
    assert [_master(store, slug) for slug in SWARMS] == [[], [], []]


@pytest.mark.feature("master-hive-drain-item")
def test_a_drain_run_as_the_hive_member_writes_no_master_item_the_relay_would_refuse(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.hive import master_notice

    registry.update(store.redis, HIVE, ["ui=no"])
    _agents(store)
    record = registry.update(
        store.redis, HIVE, ["draining=yes"], drain=registry.Drain("worker", "the hive is stopping")
    )
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "cluster")
    monkeypatch.setenv("AGENTIHOOKS_HIVE_ID", HIVE)
    monkeypatch.setenv("AGENTIHOOKS_HIVE_REDIS_URL", "redis://hive.example:6380/2")

    master_notice.drained(store, HIVE, record)

    worlds = [InboxStore(store.redis, world=world) for world in ("", HIVE)]
    assert [inbox.inbox(f"master@{slug}") for inbox in worlds for slug in SWARMS] == [[]] * 6


@pytest.fixture
def hub(store: RedisStore) -> Iterator[SimpleNamespace]:
    """The hub hive loss pass on an in memory bus, over the three swarm store, with its exit publisher stubbed."""
    from scripts.events import memory
    from scripts.events.config import BusConfig
    from scripts.hub import hive_loss

    clock = Mock(return_value=T0)
    bus = cast(Any, memory.open_bus(BusConfig(), clock=clock))
    loss = hive_loss.HiveLoss(bus, clock, store)
    loss._emit = Mock()
    yield SimpleNamespace(bus=bus, clock=clock, loss=loss)
    bus.close()


def _start(hub: SimpleNamespace, swarm: str, name: str, execution: str) -> None:
    from scripts.events.envelope import Envelope

    claim = f"claim-{execution}"
    hub.bus.state("spoke-work").create(claim, json.dumps({"state": "started", "claim_revision": 1}).encode())
    data = {"claim_key": claim, "claim_revision": 1, "hive": LOST_HIVE, "task": f"task-{execution}", "name": name}
    started = Envelope(
        f"started:{execution}", "execution.started", swarm, "execution", execution, 1, T0, LOST_HIVE, data
    )
    hub.loss.handle(started)


def _poll_until(hub: SimpleNamespace, to: int) -> None:
    while hub.clock() < to:
        hub.clock.return_value = min(hub.clock() + 5_000, to)
        hub.loss.poll()


@pytest.mark.feature("master-hive-drain-item")
def test_a_lost_hive_tells_the_master_of_each_swarm_it_ran_once_and_keeps_the_hub_log(
    store: RedisStore, hub: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.hive import presence

    _start(hub, "sw", "engineer@1", "execution-a")
    _start(hub, "sw", "ci@2", "execution-b")
    _start(hub, "other", "engineer@3", "execution-c")
    presence.beat(hub.bus, LOST_HIVE, T0)

    _poll_until(hub, T0 + presence.TTL_MS + 30_000)

    ended_sw = "execution execution-a of task task-execution-a, execution execution-b of task task-execution-b"
    ended_other = "execution execution-c of task task-execution-c"
    [item] = _master(store, "sw")
    assert (item.sender, item.fyi) == ("swarm", True)
    assert item.text == (
        f"hive {LOST_HIVE} was lost: hive-lost. The hub ended {ended_sw}. Agents of this swarm on it: ci@2, engineer@1"
    )
    assert _texts(store, "other") == [
        f"hive {LOST_HIVE} was lost: hive-lost. The hub ended {ended_other}. Agents of this swarm on it: engineer@3"
    ]
    assert capsys.readouterr().err.splitlines() == [
        f"hub: hive {LOST_HIVE} of swarm sw lost; exited {ended_sw}",
        f"hub: hive {LOST_HIVE} of swarm other lost; exited {ended_other}",
    ]


@pytest.mark.feature("master-hive-drain-item")
def test_a_failed_master_send_still_logs_every_lost_hive_first(
    hub: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.hive import master_notice, presence

    monkeypatch.setattr(master_notice, "lost", Mock(side_effect=ConnectionError("redis is down")))
    _start(hub, "sw", "engineer@1", "execution-a")
    _start(hub, "other", "engineer@3", "execution-c")
    presence.beat(hub.bus, LOST_HIVE, T0)

    with pytest.raises(ConnectionError):
        _poll_until(hub, T0 + presence.TTL_MS)

    assert capsys.readouterr().err.splitlines() == [
        f"hub: hive {LOST_HIVE} of swarm sw lost; exited execution execution-a of task task-execution-a",
        f"hub: hive {LOST_HIVE} of swarm other lost; exited execution execution-c of task task-execution-c",
    ]
