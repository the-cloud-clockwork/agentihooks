import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.events.config import BusConfig, QueuePolicy
from scripts.events.envelope import Envelope
from scripts.events.memory import open_bus
from scripts.hive import lanes, spoke
from scripts.hub import slots
from scripts.swarm_v2.runtime.base import Outcome, Status


def offer(task="t1", lane="eng", generation=2):
    data = {"lane": lane, "generation": generation}
    return Envelope(
        f"ready:rig:{task}:{generation}", "task.ready", "rig", "task", task, generation, 9, "readiness", data
    )


class Hub:
    def __init__(self, admitted):
        self.admitted, self.seen = admitted, []

    def admit(self, slug, request):
        self.seen.append(("admit", slug, request))
        return self.admitted

    def reopen(self, slug, request):
        self.seen.append(("reopen", slug, request))
        return True


@pytest.fixture
def hub():
    bus = open_bus(BusConfig(scoped=True))
    admitted = {
        "hive": "hive-a",
        "name": "eng@rig-0001",
        "task": {
            "id": "t1",
            "seat": "eng-1@rig",
            "execution_id": "x1",
            "execution_generation": 1,
            "claim_generation": 2,
        },
    }
    served = Hub(admitted)
    slots.serve(bus, admitter=served)
    yield SimpleNamespace(bus=bus, hub=served, admitted=admitted)
    bus.close()


def test_lane_work_asks_the_hub_to_admit_it_and_spawns_the_admitted_task(hub):
    from scripts.swarm.store import SwarmConfig

    config = SwarmConfig("rig", "/repo", 1, 1)
    configs = Mock(return_value=config)
    request = lanes.LaneTasks(hub.bus, "hive-a", configs).request(offer())

    assert hub.hub.seen == [
        ("admit", "rig", {"op": "admit", "hive": "hive-a", "task": "t1", "lane": "eng", "generation": 2})
    ]
    configs.assert_called_once_with("rig")
    assert (request.config, request.lane, request.name, request.task) == (
        config,
        "eng",
        "eng@rig-0001",
        hub.admitted["task"],
    )


def test_an_offer_the_hub_withdraws_spawns_nothing(hub):
    hub.hub.admitted = None
    configs = Mock()
    assert lanes.LaneTasks(hub.bus, "hive-a", configs).request(offer()) is None
    configs.assert_not_called()


def test_dead_lane_work_asks_the_hub_to_reopen_its_task(hub):
    lanes.LaneTasks(hub.bus, "hive-a", Mock()).reopen(offer(lane="plan"))
    assert hub.hub.seen == [
        ("reopen", "rig", {"op": "reopen", "hive": "hive-a", "task": "t1", "lane": "plan", "generation": 2})
    ]


@pytest.mark.parametrize(
    ("excluded", "taken"),
    [
        (None, ("eng", "ci", "plan")),
        ("", ("eng", "ci", "plan")),
        ("master", ("eng", "ci", "plan")),
        ("master,ci", ("eng", "plan")),
        ("eng,ci,plan", ()),
    ],
)
def test_a_hive_runs_every_lane_spoke_it_does_not_exclude(excluded, taken):
    assert lanes.lanes(excluded) == taken


def configs(engines):
    from scripts.swarm.store import SwarmConfig

    return SimpleNamespace(
        slugs=lambda: list(engines), config=lambda slug: SwarmConfig(slug, "/repo", 1, 1, engine=engines[slug])
    )


def test_lane_spokes_run_per_events_swarm_and_lane_and_are_kept_across_passes(capsys):
    engines = {"a": "events", "legacy": "legacy", "b": "events"}
    store, bus, runtime, held = configs(engines), Mock(), Mock(), {}
    bus.for_swarm.return_value = bus

    first = lanes.spokes(store, bus, runtime, "hive-a", ("eng", "ci"), held)

    assert [(s.config.swarm.slug, s.config.lane, s.config.hive) for s in first] == [
        ("a", "eng", "hive-a"),
        ("a", "ci", "hive-a"),
        ("b", "eng", "hive-a"),
        ("b", "ci", "hive-a"),
    ]
    assert all(s.runtime is runtime and isinstance(s.tasks, lanes.LaneTasks) and s.view() is None for s in first)
    logged = capsys.readouterr().err.splitlines()
    assert logged == [
        "hive spoke for swarm a lane eng takes swarm.a.ready.eng",
        "hive spoke for swarm a lane ci takes swarm.a.ready.ci",
        "hive spoke for swarm b lane eng takes swarm.b.ready.eng",
        "hive spoke for swarm b lane ci takes swarm.b.ready.ci",
    ]
    del engines["a"]
    engines["legacy"] = "events"
    second = lanes.spokes(store, bus, runtime, "hive-a", ("eng", "ci"), held)
    assert second[2:] == first[2:]
    assert [(s.config.swarm.slug, s.config.lane) for s in second] == [
        ("legacy", "eng"),
        ("legacy", "ci"),
        ("b", "eng"),
        ("b", "ci"),
    ]
    assert len(capsys.readouterr().err.splitlines()) == 2


def test_a_lane_spoke_takes_an_offer_without_a_ledger_read_and_spawns_what_the_hub_admitted(hub, monkeypatch):
    from scripts.swarm import readiness

    monkeypatch.setattr(spoke.Spoke, "_measure", lambda self: {"cpus": 32, "load5": 0})
    monkeypatch.setattr(readiness.Source, "snapshot", Mock(side_effect=AssertionError("a hive reads no ledger")))
    hub.bus.queue("swarm.rig.ready.eng", QueuePolicy()).offer(offer())
    runtime = Mock()
    runtime.spawn.return_value = Outcome(
        "spawn", Status.OK, "local", SimpleNamespace(pane_id="pane-1", harness="claude", account="slot-a")
    )
    store = configs({"rig": "events"})

    assert lanes.run_once(store, hub.bus, runtime, "hive-a", ("eng",), 0, {}) == ["started"]
    [request] = [call.args[0] for call in runtime.spawn.call_args_list]
    assert (request.lane, request.name, request.task["execution_id"]) == ("eng", "eng@rig-0001", "x1")
    claim = hub.bus.for_swarm("rig").state("spoke-work").get(spoke.claim_key(offer()))
    assert json.loads(claim.value)["state"] == "started"


def test_a_hive_event_launch_resolves_the_origin_to_its_own_checkout(hub, tmp_path, monkeypatch):
    from scripts.hive.daemon import local_checkout
    from scripts.swarm.store import SwarmConfig

    root = tmp_path / "hive-repos"
    checkout = root / "project"
    git = checkout / ".git"
    git.mkdir(parents=True)
    (git / "config").write_text('[remote "origin"]\n\turl = https://github.com/team/project.git\n')
    monkeypatch.setenv("HIVE_REPO_ROOT", str(root))
    missing = tmp_path / "workstation-only"
    config = SwarmConfig("rig", str(missing), 1, 1, repo_origin="github.com/team/project")

    request = lanes.LaneTasks(hub.bus, "hive-a", lambda slug: config).request(offer())

    assert request.config.repo == "github.com/team/project"
    assert local_checkout(request.config.repo) == str(checkout)
    assert config.repo == str(missing)
    assert (request.lane, request.name, request.task) == ("eng", "eng@rig-0001", hub.admitted["task"])


def test_the_lane_loop_waits_only_after_a_pass_with_nothing_started(monkeypatch):
    outcomes = iter([["empty"], ["started", "held"], []])
    seen = []
    monkeypatch.setattr(lanes, "run_once", lambda *args: seen.append(args) or next(outcomes))
    waits = []

    lanes.run("store", "bus", "runtime", "hive", ("eng",), waits.append, passes=range(3))

    assert waits == [lanes.IDLE_S, lanes.IDLE_S]
    assert [args[:6] for args in seen] == [("store", "bus", "runtime", "hive", ("eng",), lanes.TAKE_MS)] * 3
    assert seen[0][6] == {}
    assert seen[0][6] is seen[2][6]


def test_each_lane_pass_polls_every_spoke_with_the_given_timeout(monkeypatch):
    polled = [Mock(**{"poll.return_value": "empty"}), Mock(**{"poll.return_value": "started"})]
    monkeypatch.setattr(lanes, "spokes", lambda *args: polled)

    assert lanes.run_once("store", "bus", "runtime", "hive", ("eng",), 17, {}) == ["empty", "started"]
    assert [spoke.poll.call_args.args for spoke in polled] == [(17,), (17,)]


def test_lane_spokes_start_only_in_the_distributed_deployment(monkeypatch):
    started = []
    monkeypatch.setattr(lanes, "_serve", lambda environ: started.append(environ))

    assert lanes.serve({"AGENTIHOOKS_DEPLOYMENT": "local"}) is None
    assert lanes.serve({}) is None
    environ = {"AGENTIHOOKS_DEPLOYMENT": "distributed", "AGENTIHOOKS_BUS": "nats", "HIVE_EXCLUDED": "master"}
    thread = lanes.serve(environ)
    thread.join(5)

    assert thread.daemon and thread.name == "lane-spokes"
    assert started == [environ]


@pytest.mark.parametrize("bus", [{}, {"AGENTIHOOKS_BUS": ""}, {"AGENTIHOOKS_BUS": "memory"}])
def test_a_distributed_hive_refuses_lane_spokes_on_the_memory_bus_and_says_why(monkeypatch, capsys, bus):
    started = []
    monkeypatch.setattr(lanes, "_serve", lambda environ: started.append(environ))

    assert lanes.serve({"AGENTIHOOKS_DEPLOYMENT": "distributed", **bus}) is None

    assert started == []
    assert capsys.readouterr().err == (
        "hive lane spokes refused: the memory bus never sees the hub's ready offers; "
        "a distributed hive needs AGENTIHOOKS_BUS=nats and AGENTIHOOKS_BUS_URL\n"
    )


def test_a_failing_retry_wait_is_logged_and_the_loop_sleeps_and_starts_again(monkeypatch, capsys):
    attempts = iter([RuntimeError("nats down"), RuntimeError("nats down"), None])
    loops = []

    def fail(*args):
        loops.append(args)
        error = next(attempts)
        if error:
            raise error
        raise SystemExit

    def wait(delay):
        raise ConnectionError("redis down")

    monkeypatch.setattr(lanes, "_loop", fail)
    sleeps = []
    with pytest.raises(SystemExit):
        lanes._serve({}, wait, sleeps.append)

    assert sleeps == [lanes.RETRY_S, lanes.RETRY_S]
    assert len(loops) == 3
    assert "hive lane spokes retry wait failed (ConnectionError: redis down)" in capsys.readouterr().err


def test_a_failed_lane_loop_is_logged_and_started_again(monkeypatch, capsys):
    attempts = iter([RuntimeError("nats down"), None])

    loops = []

    def fail(*args):
        loops.append(args)
        error = next(attempts)
        if error:
            raise error
        raise SystemExit

    monkeypatch.setattr(lanes, "_loop", fail)
    waits = []
    with pytest.raises(SystemExit):
        lanes._serve({"HIVE_EXCLUDED": "master"}, waits.append)

    assert waits == [lanes.RETRY_S]
    assert loops == [({"HIVE_EXCLUDED": "master"},)] * 2
    assert "lane spokes stopped (RuntimeError: nats down); starting again" in capsys.readouterr().err


def test_the_lane_loop_binds_the_bus_from_its_environment_and_runs_as_this_hive(monkeypatch):
    from scripts.events import factory
    from scripts.swarm import commands, store
    from scripts.swarm import runtime as herdr

    bus = Mock()
    configs, served = [], []
    monkeypatch.setattr(factory, "open_bus", lambda config: configs.append(config) or bus)
    monkeypatch.setattr(store, "connect", lambda: "swarm store")
    monkeypatch.setattr(commands, "hive_id", lambda: "hive-a")
    monkeypatch.setattr(herdr, "HerdrRuntime", lambda: "herdr")
    monkeypatch.setattr(lanes, "run", lambda *args: served.append(args))
    environ = {"AGENTIHOOKS_BUS": "nats", "AGENTIHOOKS_BUS_URL": "nats://bus:4222", "HIVE_EXCLUDED": "master,ci"}

    lanes._loop(environ)

    assert [(c.backend, c.url, c.provision) for c in configs] == [("nats", "nats://bus:4222", False)]
    [(seen_store, seen_bus, runtime, hive, taken)] = served
    assert (seen_store, seen_bus, hive, taken) == ("swarm store", bus, "hive-a", ("eng", "plan"))
    assert (type(runtime).__name__, runtime.herdr) == ("LocalHerdrRuntime", "herdr")
    bus.close.assert_called_once_with()


def test_the_lane_loop_reads_no_excluded_lanes_as_every_lane(monkeypatch):
    from scripts.events import factory
    from scripts.swarm import commands, store
    from scripts.swarm import runtime as herdr

    served = []
    monkeypatch.setattr(factory, "open_bus", lambda config: Mock())
    monkeypatch.setattr(store, "connect", lambda: "swarm store")
    monkeypatch.setattr(commands, "hive_id", lambda: "hive-a")
    monkeypatch.setattr(herdr, "HerdrRuntime", lambda: "herdr")
    monkeypatch.setattr(lanes, "run", lambda *args: served.append(args))

    lanes._loop({})

    assert served[0][4] == lanes.LANES


def test_hive_run_starts_the_lane_spokes_before_the_telemetry_loop(monkeypatch):
    import os

    from scripts.hive import cli, daemon

    order = []
    monkeypatch.setattr(lanes, "serve", lambda environ: order.append(("lanes", environ is os.environ)))
    monkeypatch.setattr(daemon, "run", lambda connect: order.append(("telemetry",)))

    assert cli._daemon("run") == 0
    assert order == [("lanes", True), ("telemetry",)]
