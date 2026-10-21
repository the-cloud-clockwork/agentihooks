import argparse
import json
from dataclasses import replace

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm.store import MASTER, AgentRecord, SwarmError
from scripts.swarm.tick import STARTUP_GRACE_MS, Placed, tick
from tests.swarm.test_tick import FakeRuntime, masters, store, tasks  # noqa: F401


class RecoveryRuntime(FakeRuntime):
    def recover(self, name):
        return Placed("hand:p1", "codex")


@pytest.mark.parametrize("record", ["stale", "missing"])
def test_tick_recovers_live_master_without_spawning_or_moving_mail(store, record):  # noqa: F811
    store.update("sw", state="paused")
    dead = store.next_name("sw", MASTER)
    live = store.next_name("sw", MASTER)
    seat = "master@sw"
    if record == "stale":
        store.put_agent("sw", AgentRecord(dead, MASTER, MASTER, seat=seat))
    store.seats.occupy(seat, live, 1)
    inbox = InboxStore(store.redis)
    item = inbox.send("operator", live, "Keep working")
    generation = store.seats.occupant(seat).generation
    runtime = RecoveryRuntime()
    runtime.live.add(live)

    actions = tick("sw", store, tasks(), runtime, STARTUP_GRACE_MS + 2)

    assert runtime.masters == []
    assert [a.name for a in masters(store)] == [live]
    assert masters(store)[0].pane_id == "hand:p1"
    assert masters(store)[0].harness == "codex"
    assert store.seats.occupant(seat).generation == generation
    assert inbox.get(item.id).address == live
    assert f"adopted live master {live}" in actions
    tick("sw", store, tasks(), runtime, STARTUP_GRACE_MS + 3)
    assert runtime.masters == []


def test_tick_recovers_a_master_whose_seat_was_restored_to_a_dead_occupant(store):  # noqa: F811
    dead = store.next_name("sw", MASTER)
    live = store.next_name("sw", MASTER)
    store.put_agent("sw", AgentRecord(dead, MASTER, MASTER, seat="master@sw"))
    store.seats.occupy("master@sw", dead, 1)
    runtime = RecoveryRuntime()
    runtime.live.add(live)

    tick("sw", store, tasks(), runtime, STARTUP_GRACE_MS + 2)

    assert runtime.masters == []
    assert [a.name for a in masters(store)] == [live]
    assert store.seats.occupant("master@sw").occupant == live


def test_a_live_lead_in_its_seat_keeps_recovery_from_adopting_another_master_pane(store):  # noqa: F811
    lead = store.next_name("sw", MASTER)
    store.put_agent("sw", AgentRecord(lead, MASTER, MASTER, seat="master@sw"))
    store.seats.occupy("master@sw", lead, 1)
    runtime = RecoveryRuntime()
    runtime.live.update({lead, "sw-master-7"})

    actions = tick("sw", store, tasks(), runtime, STARTUP_GRACE_MS + 2)

    assert not [a for a in actions if a.startswith("adopted live master")]
    assert [a.name for a in masters(store)] == [lead]


@pytest.mark.parametrize("name", ["hand-opened-master", "sw-master-7"])
def test_recovery_uses_the_live_seat_occupant_without_a_registered_name(store, name):  # noqa: F811
    store.seats.occupy("master@sw", name, 1)
    runtime = RecoveryRuntime()
    runtime.live.add(name)
    tick("sw", store, tasks(), runtime, STARTUP_GRACE_MS + 2)
    assert runtime.masters == []
    assert [a.name for a in masters(store)] == [name]


def test_recovery_rebinds_a_live_master_whose_execution_lost_its_agent_record(store):  # noqa: F811
    live = store.next_name("sw", MASTER)
    admitted = store.start_execution("sw", AgentRecord(live, MASTER, MASTER, started_at=5, seat="master@sw"))
    store.redis.hdel(store.key("sw", "agents"), live)
    store.seats.occupy("master@sw", live, 1)
    runtime = RecoveryRuntime()
    runtime.live.add(live)

    actions = tick("sw", store, tasks(), runtime, STARTUP_GRACE_MS + 2)

    assert f"adopted live master {live}" in actions
    [master] = masters(store)
    assert (master.execution_id, master.generation, master.started_at) == (admitted.execution_id, 1, 5)
    assert (master.pane_id, master.harness, master.state) == ("hand:p1", "codex", "working")
    assert store.execution_identity_conflicts_total("sw") == 0
    with pytest.raises(SwarmError, match="changed execution identity"):
        store.put_agent("sw", replace(master, started_at=6))


def test_recovery_never_gives_a_live_master_another_masters_execution(store):  # noqa: F811
    dead = store.next_name("sw", MASTER)
    live = store.next_name("sw", MASTER)
    admitted = store.start_execution("sw", AgentRecord(dead, MASTER, MASTER, started_at=5, seat="master@sw"))
    store.redis.hdel(store.key("sw", "agents"), dead)
    store.seats.occupy("master@sw", live, 1)
    runtime = RecoveryRuntime()
    runtime.live.add(live)

    actions = tick("sw", store, tasks(), runtime, STARTUP_GRACE_MS + 2)

    assert f"adopted live master {live}" in actions
    [master] = masters(store)
    assert (master.name, master.execution_id, master.started_at) == (live, "", STARTUP_GRACE_MS + 2)
    assert (master.task, master.seat) == (MASTER, "master@sw")
    kept = store.execution("sw", admitted.execution_id)
    assert (kept.name, kept.started_at) == (dead, 5)


def test_stale_pane_on_a_dead_record_is_not_closed_during_recovery(store):  # noqa: F811
    dead = store.next_name("sw", MASTER)
    live = store.next_name("sw", MASTER)
    store.put_agent("sw", AgentRecord(dead, MASTER, MASTER, pane_id="hand:p1"))
    store.seats.occupy("master@sw", live, 1)
    runtime = RecoveryRuntime()
    runtime.live.add(live)
    tick("sw", store, tasks(), runtime, STARTUP_GRACE_MS + 2)
    assert "hand:p1" not in runtime.closed


def test_a_stopped_swarm_recovers_its_live_master_before_reaping_on_wake(store):  # noqa: F811
    store.update("sw", state="stopped")
    dead = store.next_name("sw", MASTER)
    live = store.next_name("sw", MASTER)
    store.put_agent("sw", AgentRecord(dead, MASTER, MASTER, pane_id="hand:p1"))
    store.seats.occupy("master@sw", live, 1)
    InboxStore(store.redis).send("operator", "master@sw", "Resume the master")
    runtime = RecoveryRuntime()
    runtime.live.add(live)

    tick("sw", store, tasks(), runtime, STARTUP_GRACE_MS + 2)

    assert "hand:p1" not in runtime.closed
    assert runtime.masters == []
    assert [a.name for a in masters(store)] == [live]
    assert store.config("sw").state == "paused"


def test_a_live_master_in_another_swarm_does_not_prevent_respawn(store):  # noqa: F811
    runtime = RecoveryRuntime()
    runtime.live.add("master@ffffff-0001")
    tick("sw", store, tasks(), runtime, STARTUP_GRACE_MS + 2)
    assert len(runtime.masters) == 1


def test_runtime_recovers_the_live_herdr_pane_and_harness():
    from scripts.swarm.runtime import HerdrRuntime

    calls = []

    def herdr(args):
        calls.append(args)
        return {"agent": {"pane_id": "hand:p1", "agent": "codex"}}

    runtime = HerdrRuntime(herdr=herdr)
    assert runtime.recover("master@a1b2c3-0001") == Placed("hand:p1", "codex")
    assert calls == [["agent", "get", "master-a1b2c3-0001"]]


def test_runtime_keeps_a_live_master_when_herdr_cannot_locate_its_pane():
    from scripts.swarm.runtime import HerdrRuntime

    def unavailable(args):
        raise RuntimeError("herdr down")

    assert HerdrRuntime(herdr=unavailable).recover("hand-opened-master") == Placed("", "")


def test_a_finished_master_is_retired_instead_of_adopted(store):  # noqa: F811
    runtime = RecoveryRuntime()
    tick("sw", store, tasks(), runtime, 1)
    old = masters(store)[0]
    store.put_agent("sw", replace(old, state="finished"))
    tick("sw", store, tasks(), runtime, 2)
    assert old.name in runtime.killed
    assert len(runtime.masters) == 2


NOW = 10_000_000
GRACE_MS = 300_000
QUIET = {"slots": [], "interactive": {}, "repos": [], "sessions": {}, "quota": {}}


def _hive(store, hive, beat):  # noqa: F811
    from scripts.hive import registry

    registry.update(store.redis, hive, ["ui=yes"])
    slots = [{"harness": "claude", "account": "one", "kind": "subscription"}]
    store.redis.hset(
        registry.key(hive),
        mapping={"heartbeat_at": beat, "slots": json.dumps(slots), "repos": json.dumps(["/repo"])},
    )


def _hive_master(store, hive="lost"):  # noqa: F811
    name = store.next_name("sw", MASTER)
    record = AgentRecord(
        name,
        MASTER,
        MASTER,
        harness="claude",
        account="one",
        seat="master@sw",
        hive=hive,
        runtime_target={"host_id": hive},
    )
    master = store.start_execution("sw", record)
    store.seats.occupy("master@sw", name, 1)
    return master


def test_a_lost_master_hive_moves_the_master_to_a_live_hive(store):  # noqa: F811
    from scripts.handoff import transfers
    from scripts.swarm import master_move

    _hive(store, "lost", NOW - GRACE_MS - 1)
    _hive(store, "spare", NOW)
    master = _hive_master(store)
    generation = store.seats.occupant("master@sw").generation
    runtime = RecoveryRuntime()

    actions = tick("sw", store, tasks(), runtime, NOW)

    assert f"moved master {master.name} off lost hive lost" in actions
    [moved] = [row for row in transfers.list_transfers(store, "sw") if row["reason"] == master_move.LOST]
    assert moved["predecessor"] == master.name
    assert moved["handoff"] == master_move.RECOVERY.format(slug="sw", name=master.name, hive="lost", seconds=300)
    assert moved["handoff"].count("unknown:") == 3
    fence = json.loads(store.redis.hget(master_move.fenced("lost"), master.execution_id))
    assert fence == {"slug": "sw", "generation": generation + 1, "at": NOW}
    assert {"generation": generation + 1, "occupant": master.name, "at": NOW} in store.seats.history("master@sw")
    [(successor, task)] = runtime.masters
    assert task["hive"] == "spare"
    assert moved["successor"] == successor
    assert master.name not in [a.name for a in masters(store) if a.state != "finished"]
    assert master_move.pending(store, "sw") is None


def test_a_lost_master_hive_without_a_live_spare_starts_an_unpinned_successor(store):  # noqa: F811
    from scripts.swarm import master_move

    _hive(store, "lost", NOW - GRACE_MS - 1)
    master = _hive_master(store)
    runtime = RecoveryRuntime()

    actions = tick("sw", store, tasks(), runtime, NOW)

    assert f"moved master {master.name} off lost hive lost" in actions
    [(_, task)] = runtime.masters
    assert "hive" not in task
    assert master_move.pending(store, "sw") is None


@pytest.mark.parametrize(
    ("beat", "live", "grace"),
    [
        (NOW - GRACE_MS, False, None),
        (NOW - GRACE_MS - 1, True, None),
        (0, False, None),
        (NOW - 400_000, False, 600),
        (None, False, None),
    ],
    ids=["inside-grace", "seen-live", "never-beat", "longer-grace", "record-gone"],
)
def test_a_master_hive_back_inside_the_grace_keeps_its_master(store, beat, live, grace):  # noqa: F811
    from scripts.handoff import transfers
    from scripts.swarm import master_move

    if grace:
        store.update("sw", master_move_grace=grace)
    if beat is not None:
        _hive(store, "lost", beat)
    _hive(store, "spare", NOW)
    master = _hive_master(store)
    generation = store.seats.occupant("master@sw").generation
    runtime = RecoveryRuntime()
    if live:
        runtime.live.add(master.name)

    actions = tick("sw", store, tasks(), runtime, NOW)

    assert not [a for a in actions if a.startswith("moved master")]
    assert transfers.list_transfers(store, "sw") == []
    assert store.seats.occupant("master@sw").generation == generation
    assert store.redis.hlen(master_move.fenced("lost")) == 0
    assert [(a.name, a.state) for a in masters(store)] == [(master.name, "working")]
    assert runtime.masters == []


class FenceRuntime:
    def __init__(self, ok=True):
        self.ok, self.terminated, self.herdr = ok, [], None

    def terminate(self, agent, homes=()):
        from scripts.hive import registry
        from scripts.swarm_v2.runtime.base import Outcome, Status

        self.terminated.append((agent.execution_id, registry.show(self.redis, "lost")["heartbeat_at"]))
        return Outcome("terminate", Status.OK if self.ok else Status.REFUSED, "local")


def _beat(store, monkeypatch, runtime):  # noqa: F811
    from scripts.hive import daemon, orders

    runtime.redis = store.redis
    monkeypatch.setattr(daemon, "observe", lambda **kwargs: QUIET)
    monkeypatch.setattr(daemon, "execution_report", lambda redis, hive, herdr: {})
    monkeypatch.setattr(orders.Consumer, "local", classmethod(lambda cls, redis, hive: cls(redis, hive, runtime)))
    monkeypatch.setattr(daemon.time, "time", lambda: NOW / 1000 + 5)
    polled = []
    monkeypatch.setattr(orders.Consumer, "poll", lambda self: polled.append(self.hive) or 0)
    daemon.beat(store.redis, "lost")
    return polled


@pytest.mark.parametrize("ok", [True, False])
def test_a_returning_hive_terminates_its_superseded_master_before_it_beats(store, monkeypatch, ok):  # noqa: F811
    from scripts.hive import registry
    from scripts.swarm import master_move

    _hive(store, "lost", NOW - GRACE_MS - 1)
    master = _hive_master(store)
    tick("sw", store, tasks(), RecoveryRuntime(), NOW)
    runtime = FenceRuntime(ok)

    polled = _beat(store, monkeypatch, runtime)

    assert runtime.terminated == [(master.execution_id, NOW - GRACE_MS - 1)]
    assert registry.show(store.redis, "lost")["heartbeat_at"] == NOW + 5000
    assert store.redis.hexists(master_move.fenced("lost"), master.execution_id) == (not ok)
    assert polled == (["lost"] if ok else [])


def test_a_fence_naming_an_unknown_execution_is_dropped(store, monkeypatch):  # noqa: F811
    from scripts.hive import registry
    from scripts.swarm import master_move

    assert master_move.fenced("lost") == f"{registry.key('lost')}:fenced"
    _hive(store, "lost", NOW)
    store.redis.hset(master_move.fenced("lost"), "exe-gone", json.dumps({"slug": "sw", "generation": 2, "at": 1}))
    runtime = FenceRuntime()

    _beat(store, monkeypatch, runtime)

    assert runtime.terminated == []
    assert store.redis.hlen(master_move.fenced("lost")) == 0


def test_an_unknown_fence_does_not_stop_the_fences_after_it(store, monkeypatch):  # noqa: F811
    from scripts.swarm import master_move

    _hive(store, "lost", NOW - GRACE_MS - 1)
    store.redis.hset(master_move.fenced("lost"), "exe-gone", json.dumps({"slug": "sw", "generation": 2, "at": 1}))
    master = _hive_master(store)
    tick("sw", store, tasks(), RecoveryRuntime(), NOW)
    runtime = FenceRuntime()

    _beat(store, monkeypatch, runtime)

    assert runtime.terminated == [(master.execution_id, NOW - GRACE_MS - 1)]
    assert store.redis.hlen(master_move.fenced("lost")) == 0


def test_master_move_orders_a_handoff_and_pins_the_successor_to_the_hive(store):  # noqa: F811
    from scripts.handoff import transfers
    from scripts.swarm import cli, master_move

    _hive(store, "home", NOW)
    _hive(store, "spare", NOW)
    master = _hive_master(store, "home")
    inbox = InboxStore(store.redis)
    args = cli.build_parser().parse_args(["sw", "master", "move", "--to", "spare"])
    assert (args.action, args.to) == ("move", "spare")
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["sw", "master", "move"])

    found = master_move.order(store, "sw", "spare", NOW)

    item = inbox.get(found["item"])
    assert item.address == "master@sw"
    assert item.text == master_move.ORDER.format(to="spare", current="home", slug="sw")
    assert found == {
        "from": "home",
        "to": "spare",
        "master": master.name,
        "item": item.id,
        "state": "ordered",
        "reason": "",
        "at": NOW,
    }
    assert master_move.pending(store, "sw") == found
    assert json.loads(store.redis.get(store.key("sw", "master-move"))) == found
    current = next(a for a in store.agents("sw") if a.name == master.name)
    transfers.record(store, "sw", current, "operator", "", NOW)
    store.put_agent("sw", replace(current, state="finished"))
    runtime = RecoveryRuntime()

    tick("sw", store, tasks(), runtime, NOW + 1)

    [(_, task)] = runtime.masters
    assert task["hive"] == "spare"
    assert master_move.pending(store, "sw") is None
    assert (inbox.get(item.id).state, inbox.get(item.id).reason) == ("done", "done: the master moved to its hive")


@pytest.mark.parametrize(
    ("to", "reason"),
    [
        ("home", "the master already runs on hive home"),
        ("dead", "hive dead is not live"),
        ("absent", "hive absent is not live"),
        ("bare", "the master cannot move to hive bare: no hive for master advertises the claude slot one"),
    ],
)
def test_master_move_refuses_a_hive_that_cannot_take_the_master(store, to, reason):  # noqa: F811
    from scripts.hive import registry
    from scripts.swarm import master_move
    from scripts.swarm.store import SwarmError

    _hive(store, "home", NOW)
    _hive(store, "dead", NOW - 100_000)
    registry.update(store.redis, "bare", ["ui=yes"])
    store.redis.hset(registry.key("bare"), "heartbeat_at", NOW)
    _hive_master(store, "home")

    with pytest.raises(SwarmError) as refused:
        master_move.order(store, "sw", to, NOW)

    assert str(refused.value) == reason
    assert master_move.pending(store, "sw") is None


@pytest.mark.parametrize(
    ("listed", "refusal"),
    [
        ("github.com/the-cloud-clockwork/agentihooks", None),
        ("github.com/o/other", "no hive for master lists the repo {repo}"),
    ],
)
def test_master_move_matches_a_local_checkout_by_the_origin_the_hive_lists(store, tmp_path, listed, refusal):  # noqa: F811
    from scripts.hive import registry
    from scripts.swarm import master_move

    checkout = tmp_path / "agentihooks"
    (checkout / ".git").mkdir(parents=True)
    (checkout / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/the-cloud-clockwork/agentihooks.git\n'
    )
    store.redis.hset(store.key("sw", "config"), "repo", str(checkout))
    _hive(store, "home", NOW)
    _hive(store, "spare", NOW)
    store.redis.hset(registry.key("spare"), "repos", json.dumps([listed]))
    _hive_master(store, "home")

    if refusal is None:
        assert master_move.order(store, "sw", "spare", NOW)["to"] == "spare"
        return
    with pytest.raises(SwarmError) as refused:
        master_move.order(store, "sw", "spare", NOW)
    assert str(refused.value) == f"the master cannot move to hive spare: {refusal.format(repo=checkout)}"


def test_an_ordered_move_never_pins_a_master_that_left_without_handing_off(store):  # noqa: F811
    from scripts.swarm import master_move

    _hive(store, "home", NOW)
    _hive(store, "spare", NOW)
    master = _hive_master(store, "home")
    master_move.order(store, "sw", "spare", NOW)
    current = next(a for a in store.agents("sw") if a.name == master.name)
    store.put_agent("sw", replace(current, state="finished"))
    runtime = RecoveryRuntime()

    tick("sw", store, tasks(), runtime, NOW + 1)

    [(_, task)] = runtime.masters
    assert "hive" not in task


def test_the_lost_hive_move_places_the_master_on_the_hive_with_room(store):  # noqa: F811
    from scripts.hive import registry
    from tests.hive.test_controller_launch import publish_room

    _hive(store, "lost", NOW - GRACE_MS - 1)
    for hive, room in (("big", 0), ("small", 2)):
        _hive(store, hive, NOW)
        store.redis.hset(registry.key(hive), "max_agents", 8)
        publish_room(store, hive, room)
    _hive_master(store)
    runtime = RecoveryRuntime()

    tick("sw", store, tasks(), runtime, NOW)

    [(_, task)] = runtime.masters
    assert task["hive"] == "small"


def test_a_failed_successor_launch_keeps_the_recovery_handoff_awaiting(store):  # noqa: F811
    from scripts.handoff import transfers
    from scripts.swarm import master_move

    _hive(store, "lost", NOW - GRACE_MS - 1)
    master = _hive_master(store)
    runtime = RecoveryRuntime(fail=True)

    tick("sw", store, tasks(), runtime, NOW)

    rows = [row for row in transfers.list_transfers(store, "sw") if row["reason"] == master_move.LOST]
    assert [row["binding"]["state"] for row in rows] == ["absent", "pending"]
    assert rows[-1]["successor"] == ""
    assert rows[-1]["predecessor"] == master.name
    assert rows[-1]["continuity"] == {"state": "pending"}
    assert master_move.pending(store, "sw") == {
        "from": "lost",
        "to": "",
        "master": master.name,
        "state": "moving",
        "at": NOW,
    }
    assert (store.handoff("sw", MASTER), store.handoff_seat("sw", MASTER)) == (rows[0]["handoff"], "master@sw")


def test_master_move_without_a_live_master_is_refused(store):  # noqa: F811
    from scripts.swarm import master_move
    from scripts.swarm.store import SwarmError

    _hive(store, "spare", NOW)
    with pytest.raises(SwarmError) as refused:
        master_move.order(store, "sw", "spare", NOW)
    assert str(refused.value) == "no live master to move"


def test_master_move_keeps_an_interactive_master_on_a_hive_holding_its_login(store):  # noqa: F811
    from scripts.hive import registry
    from scripts.swarm import master_move
    from scripts.swarm.store import SwarmError

    _hive(store, "spare", NOW)
    _hive(store, "login", NOW)
    store.redis.hset(registry.key("login"), "interactive", json.dumps({"claude": "one", "codex": ""}))
    name = store.next_name("sw", MASTER)
    store.put_agent(
        "sw",
        AgentRecord(name, MASTER, MASTER, harness="claude", account="one", seat="master@sw", route_kind="interactive"),
    )

    with pytest.raises(SwarmError) as refused:
        master_move.order(store, "sw", "spare", NOW)

    assert (
        str(refused.value) == "the master cannot move to hive spare: no hive for master holds the claude login for one"
    )
    assert master_move.order(store, "sw", "login", NOW)["to"] == "login"


def test_master_move_from_the_command_line_replaces_an_earlier_order(store, capsys, monkeypatch):  # noqa: F811
    from scripts.swarm import cli, master_move

    _hive(store, "spare", NOW)
    _hive(store, "other", NOW)
    name = store.next_name("sw", MASTER)
    store.put_agent("sw", AgentRecord(name, MASTER, MASTER, harness="claude", account="one", seat="master@sw"))
    first = master_move.order(store, "sw", "other", NOW)
    monkeypatch.setattr(cli, "now_ms", lambda: NOW)

    cli.cmd_master(store, cli.build_parser().parse_args(["sw", "master", "move", "--to", "spare"]))

    printed = json.loads(capsys.readouterr().out)
    assert printed == master_move.pending(store, "sw")
    assert (printed["to"], printed["master"]) == ("spare", name)
    inbox = InboxStore(store.redis)
    assert (inbox.get(first["item"]).state, inbox.get(first["item"]).reason) == (
        "cancelled",
        "cancelled: the master move was replaced",
    )
    assert inbox.get(printed["item"]).text == master_move.ORDER.format(to="spare", current="this machine", slug="sw")


def test_master_move_grace_round_trips(store):  # noqa: F811
    from scripts.swarm import cli

    store.update("sw", state="paused")
    assert store.config("sw").master_move_grace == 300

    cli.cmd_set(store, argparse.Namespace(slug="sw", pairs=["master-move-grace=600"]))

    assert store.config("sw").master_move_grace == 600
    assert cli.setting(store.config("sw"), "master-move-grace") == 600
