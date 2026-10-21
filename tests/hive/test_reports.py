import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.unit

HIVE = "remote"
PANE = "w1:p1"
CONVERSATION = "8c1f5a7e-3b9d-4c2e-9f61-0d4b2a7c5e13"
BINDING = {"profile": "engineer", "harness": "claude", "account": "account", "hooks": True}


def _herdr(args):
    raise RuntimeError("the local herdr server holds no remote pane")


@pytest.fixture
def remote(monkeypatch, request):
    import fakeredis

    from scripts import terminate_agent
    from scripts.hive import registry, reports
    from scripts.swarm.runtime import HerdrRuntime
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

    params = getattr(request, "param", {})
    monkeypatch.setenv("SWARM_HIVE_ID", "controller")
    monkeypatch.setattr(terminate_agent, "sessions", lambda *args, **kwargs: [])
    redis = fakeredis.FakeRedis(decode_responses=True)
    store = RedisStore(redis)
    store.create(SwarmConfig("fixture", "agentihooks", 1, 0))
    agent = store.start_execution(
        "fixture",
        AgentRecord(
            store.next_name("fixture", "eng"),
            "eng",
            "task",
            pane_id=PANE,
            seat="eng-1@fixture",
            harness="claude",
            profile="engineer",
            account="account",
            started_at=params.get("started_at", 0),
            launched_at=params.get("launched_at", 0),
            hive=HIVE,
            runtime_target={"host_id": HIVE, "process_namespace": "theirs", "pid": 12, "pid_start": 34},
            profile_decision={"validation": {"pid": 12}},
        ),
    )
    store.put_agent("fixture", agent)
    redis.sadd(registry.INDEX, HIVE)
    runtime = HerdrRuntime(herdr=_herdr)
    runtime.hive = reports.Reports(redis)
    return SimpleNamespace(redis=redis, store=store, agent=agent, runtime=runtime, reports=reports)


def _publish(setup, **pane):
    entry = {
        "slug": "fixture",
        "name": setup.agent.name,
        "generation": setup.agent.generation,
        "reported": True,
        "binding": BINDING,
        "pane": {"state": "working", **pane},
    }
    report = {
        "names": [setup.agent.name],
        "panes": {PANE: CONVERSATION},
        "executions": {setup.agent.execution_id: entry},
    }
    setup.redis.set(setup.reports.key(HIVE), json.dumps(report), ex=45)


def test_remote_host_is_another_hive_than_this_one(remote):
    from scripts.swarm.store import AgentRecord

    assert remote.reports.host(remote.agent) == HIVE
    assert remote.reports.pane_key(remote.agent) == f"{HIVE}/{PANE}"
    here = replace(remote.agent, runtime_target={"host_id": "controller"})
    assert remote.reports.host(here) == ""
    assert remote.reports.pane_key(here) == PANE
    assert remote.reports.host(AgentRecord("local", "eng", "task", pane_id=PANE)) == ""
    assert remote.reports.host(replace(remote.agent, runtime_backend="kubernetes")) == ""


def test_a_remote_claude_worker_is_seen_live_from_its_hive_report(remote):
    _publish(remote, typed="half a line", provider_error="overloaded", provider_error_id="529")
    runtime, agent = remote.runtime, remote.agent
    assert agent.name in runtime.live_names()
    assert runtime.reported(agent) is True
    observed = runtime.observe(agent)
    assert observed.state == "working"
    assert observed.typed == "half a line"
    assert observed.provider_error == "overloaded"
    assert observed.provider_error_id == "529"
    assert runtime.status(agent) == "working"
    assert runtime.bindings([agent]) == {agent.name: BINDING}
    assert runtime.conversations() is None
    runtime.herdr = lambda args: {"agents": [{"pane_id": PANE, "agent_session": {"kind": "id", "value": "local"}}]}
    assert runtime.conversations() == {PANE: "local"}


def test_the_tick_records_a_remote_conversation_when_the_local_listing_fails(remote):
    from scripts.swarm import tick
    from scripts.swarm.store import AgentRecord

    local = remote.store.start_execution(
        "fixture",
        AgentRecord(
            remote.store.next_name("fixture", "eng"),
            "eng",
            "other",
            pane_id="w1:p9",
            seat="eng-2@fixture",
            conversation_id="kept",
        ),
    )
    remote.store.put_agent("fixture", local)
    _publish(remote)
    assert remote.runtime.conversations() is None
    tick._conversations("fixture", remote.store, remote.runtime)
    found = {a.name: a.conversation_id for a in remote.store.agents("fixture")}
    assert found == {remote.agent.name: CONVERSATION, local.name: "kept"}


def test_a_remote_worker_is_not_reaped_while_its_hive_reports_it(remote):
    from scripts.swarm import idle, tick

    _publish(remote)
    remote.store.claim("fixture", "task", remote.agent.name, 60000)
    rows = {"task": {"state": "claimed", "claimed_by": remote.agent.name}}
    now = remote.agent.started_at + 10**9
    idle.beat(remote.store.redis, "fixture", remote.agent.name, idle.WORKING, now)
    actions = tick._reap("fixture", remote.store, Mock(), remote.runtime, rows, now)
    assert actions == []
    kept = remote.store.agents("fixture")
    assert [a.name for a in kept] == [remote.agent.name]
    assert kept[0].state == "working"
    assert remote.store.claimant("fixture", "task") == remote.agent.name


def test_a_lost_hive_marks_its_executions_unknown_not_dead(remote):
    from scripts.swarm import tick
    from scripts.swarm.tick import SUSPECT

    runtime, agent = remote.runtime, remote.agent
    assert agent.name not in runtime.live_names()
    assert runtime.observe(agent).state == "unknown"
    assert runtime.reported(agent) is False
    assert runtime.bindings([agent]) == {}
    remote.store.claim("fixture", "task", agent.name, 60000)
    rows = {"task": {"state": "claimed", "claimed_by": agent.name}}
    actions = tick._reap("fixture", remote.store, Mock(), runtime, rows, agent.started_at + 10**9)
    assert actions == [f"suspect {agent.name}: its runtime did not answer"]
    kept = remote.store.agents("fixture")
    assert [(a.name, a.state) for a in kept] == [(agent.name, SUSPECT)]
    assert remote.store.claimant("fixture", "task") == agent.name


LOST = 10 * 60_000
NOW = 10**9


def _beat_hive(remote, age):
    from scripts.hive import registry

    if age is not None:
        remote.redis.hset(registry.key(HIVE), "heartbeat_at", 0 if age == "never" else NOW - age)


@pytest.mark.parametrize(
    ("age", "reporting", "lost"),
    [
        (LOST + 1, False, True),
        (LOST, False, False),
        (LOST + 1, True, False),
        ("never", False, False),
        (None, False, False),
    ],
)
def test_a_hive_is_lost_only_after_its_heartbeat_outlives_the_grace_window(remote, age, reporting, lost):
    _beat_hive(remote, age)
    if reporting:
        _publish(remote)
    assert remote.runtime.hive.lost(remote.agent, NOW) is lost
    here = replace(remote.agent, runtime_target={"host_id": "controller"})
    assert remote.runtime.hive.lost(here, NOW) is False


@pytest.mark.parametrize(
    ("age", "beat", "retired"),
    [
        (LOST + 1, None, True),
        (LOST + 1, LOST + 1, True),
        (LOST + 1, LOST, False),
        (LOST, None, False),
        (None, None, False),
    ],
)
def test_tick_retires_workers_of_a_hive_that_stopped_reporting(remote, age, beat, retired):
    from scripts.swarm import idle, tick
    from scripts.swarm.tick import SUSPECT

    remote.agent = replace(remote.agent, state=SUSPECT)
    remote.store.put_agent("fixture", remote.agent)
    _beat_hive(remote, age)
    if beat is not None:
        idle.beat(remote.redis, "fixture", remote.agent.name, idle.WORKING, NOW - beat)
    remote.store.claim("fixture", "task", remote.agent.name, 60000)
    rows = {"task": {"state": "claimed", "claimed_by": remote.agent.name}}
    ledger = Mock()
    ledger.update_task.return_value = {"state": "open", "claimed_by": ""}
    remote.runtime.retire = Mock(side_effect=AssertionError("a lost hive takes no process command"))

    actions = tick._reap("fixture", remote.store, ledger, remote.runtime, rows, NOW)

    if retired:
        assert remote.store.agents("fixture") == []
        assert remote.store.claimant("fixture", "task") is None
        assert remote.store.execution_registry.occupants("fixture")[remote.agent.seat].state == "ended"
        assert actions == [f"retired {remote.agent.name}: its hive stopped reporting, task task reopened"]
    else:
        assert actions == []
        assert [(a.name, a.state) for a in remote.store.agents("fixture")] == [(remote.agent.name, SUSPECT)]
        assert remote.store.claimant("fixture", "task") == remote.agent.name


@pytest.mark.parametrize(
    ("panes", "namespace", "retired"),
    [
        (["w9:p9"], "theirs", True),
        ([], "theirs", True),
        ([PANE], "restarted", True),
        ([PANE], "theirs", False),
        (None, "theirs", False),
        (None, "restarted", False),
    ],
)
@pytest.mark.parametrize("state", ["working", "suspect", "retiring"])
def test_tick_retires_only_hive_workers_proven_gone(remote, panes, namespace, retired, state):
    from scripts.swarm import idle, tick

    remote.agent = replace(remote.agent, state=state)
    remote.store.put_agent("fixture", remote.agent)
    _publish(remote, state="unknown")
    report = json.loads(remote.redis.get(remote.reports.key(HIVE)))
    report.update(pane_ids=panes, process_namespace=namespace)
    remote.redis.set(remote.reports.key(HIVE), json.dumps(report))
    remote.store.claim("fixture", "task", remote.agent.name, 60000)
    rows = {"task": {"state": "claimed", "claimed_by": remote.agent.name}}
    now = remote.agent.started_at + 10**9
    idle.beat(remote.redis, "fixture", remote.agent.name, idle.WORKING, now)
    ledger = Mock()
    ledger.update_task.return_value = {"state": "open", "claimed_by": ""}
    remote.runtime.retire = Mock(side_effect=AssertionError("a gone pane needs no remote process command"))

    actions = tick._reap("fixture", remote.store, ledger, remote.runtime, rows, now)

    if retired:
        assert remote.store.agents("fixture") == []
        assert remote.store.claimant("fixture", "task") is None
        assert remote.store.execution_registry.occupants("fixture")[remote.agent.seat].state == "ended"
        successor = remote.store.start_execution(
            "fixture",
            replace(
                remote.agent,
                name=remote.store.next_name("fixture", "eng"),
                execution_id="",
                generation=0,
                state="starting",
            ),
            remote.agent.execution_id,
        )
        assert remote.store.execution_registry.occupants("fixture")[remote.agent.seat].name == successor.name
        assert actions == [f"retired {remote.agent.name}: its hive pane is gone, task task reopened"]
    else:
        assert [agent.name for agent in remote.store.agents("fixture")] == [remote.agent.name]
        assert remote.store.claimant("fixture", "task") == remote.agent.name


def test_one_tick_releases_every_missing_hive_worker_and_keeps_each_action(remote):
    from scripts.swarm import tick

    second = remote.store.start_execution(
        "fixture",
        replace(
            remote.agent,
            name=remote.store.next_name("fixture", "eng"),
            task="second",
            seat="eng-2@fixture",
            execution_id="",
            generation=0,
        ),
    )
    remote.store.put_agent("fixture", second)
    _publish(remote, state="unknown")
    report = json.loads(remote.redis.get(remote.reports.key(HIVE)))
    report.update(pane_ids=[], process_namespace="theirs")
    remote.redis.set(remote.reports.key(HIVE), json.dumps(report))
    rows = {}
    for agent in (remote.agent, second):
        remote.store.claim("fixture", agent.task, agent.name, 60000)
        rows[agent.task] = {"state": "claimed", "claimed_by": agent.name}
    ledger = Mock()
    ledger.update_task.side_effect = lambda slug, task, patch, **kwargs: patch

    actions = tick._reap("fixture", remote.store, ledger, remote.runtime, rows, 10**9)

    assert remote.store.agents("fixture") == []
    assert actions == [
        f"retired {remote.agent.name}: its hive pane is gone, task task reopened",
        f"retired {second.name}: its hive pane is gone, task second reopened",
    ]


def test_a_gone_hive_worker_refunds_its_parked_life_and_forgets_its_retire_request(remote):
    from scripts.swarm import master_retire, tick

    _publish(remote, state="unknown")
    report = json.loads(remote.redis.get(remote.reports.key(HIVE)))
    report.update(pane_ids=[], process_namespace="theirs")
    remote.redis.set(remote.reports.key(HIVE), json.dumps(report))
    remote.store.claim("fixture", "task", remote.agent.name, 60000)
    remote.store.count_claim("fixture", "task")
    requests = master_retire._key(remote.store, "fixture")
    remote.redis.hset(requests, remote.agent.name, json.dumps({"at": 0, "item": "missing"}))
    rows = {"task": {"state": "parked", "parked_on": ["dep"]}, "dep": {"state": "claimed"}}

    actions = tick._reap("fixture", remote.store, Mock(), remote.runtime, rows, 10**9)

    assert actions == [f"retired {remote.agent.name}: its hive pane is gone"]
    assert remote.store.claims("fixture", "task") == 0
    assert remote.redis.hget(requests, remote.agent.name) is None


@pytest.mark.parametrize("handoff", [False, True])
def test_retiring_a_finished_hive_worker_records_its_inbox_exit(remote, handoff):
    from scripts.inbox.store import InboxStore
    from scripts.swarm import tick

    remote.agent = replace(remote.agent, state="finished")
    remote.store.put_agent("fixture", remote.agent)
    _publish(remote)
    if handoff:
        remote.store.put_handoff("fixture", "task", "continue the task")
    rows = {"task": {"state": "claimed" if handoff else "done", "claimed_by": remote.agent.name}}
    ledger = Mock()
    ledger.update_task.return_value = {"state": "open", "claimed_by": ""}
    remote.runtime.retire = Mock(return_value=True)

    actions = tick._reap("fixture", remote.store, ledger, remote.runtime, rows, 10**9)

    assert actions == [f"retired {remote.agent.name}"]
    assert remote.store.agents("fixture") == []
    exit_record = InboxStore(remote.redis).seats.exit_of(remote.agent.name)
    assert exit_record["reason"] == "exited"
    assert exit_record["seat"] == (remote.agent.seat if handoff else "")


@pytest.mark.parametrize("listed", [[], [{"pane_id": PANE}], None])
def test_hive_report_distinguishes_an_empty_pane_listing_from_an_outage(remote, monkeypatch, listed):
    from scripts.hive import daemon
    from scripts.swarm.pane import PaneObservation
    from scripts.swarm_v2.runtime import process

    monkeypatch.setattr(process, "local_namespace", lambda: "pod-namespace")
    herdr = Mock()
    herdr.live_names.return_value = set()
    herdr.bindings.return_value = {}
    herdr.conversations.return_value = {}
    herdr.observe.return_value = PaneObservation("unknown")
    herdr.reported.return_value = False

    def listing(args):
        assert args == ["pane", "list"]
        if listed is None:
            raise RuntimeError("hive herdr cannot be read")
        return {"panes": listed}

    herdr.herdr.side_effect = listing
    report = daemon.execution_report(remote.redis, HIVE, herdr)

    assert report["pane_ids"] == (None if listed is None else [row["pane_id"] for row in listed])
    assert report["process_namespace"] == "pod-namespace"


@pytest.mark.parametrize(
    ("remote", "stamp", "retired"),
    [
        ({"launched_at": 200}, 100, False),
        ({"started_at": 200}, 100, False),
        ({"launched_at": 1}, None, False),
        ({"started_at": 1}, None, False),
        ({"launched_at": 200}, 300, True),
        ({"started_at": 200}, 300, True),
    ],
    indirect=["remote"],
)
def test_a_hive_snapshot_from_before_launch_cannot_retire_the_new_worker(remote, stamp, retired):
    from scripts.swarm import tick

    agent = remote.agent
    _publish(remote, state="unknown")
    report = json.loads(remote.redis.get(remote.reports.key(HIVE)))
    report.update(pane_ids=[], process_namespace="restarted")
    if stamp is not None:
        report["observed_at"] = stamp
    remote.redis.set(remote.reports.key(HIVE), json.dumps(report))
    remote.store.claim("fixture", "task", agent.name, 60000)
    rows = {"task": {"state": "claimed", "claimed_by": agent.name}}

    ledger = Mock()
    ledger.update_task.return_value = {"state": "open", "claimed_by": ""}
    tick._reap("fixture", remote.store, ledger, remote.runtime, rows, agent.started_at + 10**9)

    assert [found.name for found in remote.store.agents("fixture")] == ([] if retired else [agent.name])
    assert remote.store.claimant("fixture", "task") == (None if retired else agent.name)


def test_a_hive_reporting_a_dead_process_names_it_unbound(remote):
    _publish(remote)
    report = json.loads(remote.redis.get(remote.reports.key(HIVE)))
    report["executions"][remote.agent.execution_id]["binding"] = {"process": False}
    remote.redis.set(remote.reports.key(HIVE), json.dumps(report))
    assert remote.runtime.bindings([remote.agent]) == {remote.agent.name: {"process": False}}
    stale = replace(remote.agent, generation=remote.agent.generation + 1)
    assert remote.runtime.bindings([stale]) == {}
    assert remote.runtime.observe(stale).state == "unknown"


def test_retire_goes_through_a_hive_order(remote):
    from scripts.hive.orders import Ack, CommandOrder, Consumer, Orders
    from scripts.swarm_v2.runtime.base import LOCAL, Outcome, Status

    _publish(remote)
    runtime, agent = remote.runtime, remote.agent
    assert runtime.retire(agent) is False
    assert "hive remote" in runtime.refusal(agent)["refusal"]
    assert runtime.retire(agent) is False
    assert len(remote.redis.xrange(Orders(remote.redis).commands(HIVE))) == 1
    hive = Mock()
    hive.terminate.return_value = Outcome("terminate", Status.OK, LOCAL)
    assert Consumer(remote.redis, HIVE, hive).poll_commands() == 1
    retired = hive.terminate.call_args.args[0]
    assert (retired.execution_id, retired.runtime_target) == (agent.execution_id, agent.runtime_target)
    assert runtime.retire(agent) is True
    assert agent.name not in runtime.refusals
    order = CommandOrder(f"retire:{agent.execution_id}:{agent.generation}", "retire")
    assert Orders(remote.redis).command(HIVE, order) == Ack("done", agent.runtime_target)


def test_local_runtime_terminates_a_remote_worker_by_order(remote):
    from scripts.hive.orders import Orders
    from scripts.swarm_v2.runtime.local import LocalHerdrRuntime

    _publish(remote)
    outcome = LocalHerdrRuntime(remote.runtime).terminate(remote.agent)
    assert not outcome.ok
    assert "hive remote" in outcome.detail
    ((_, fields),) = remote.redis.xrange(Orders(remote.redis).commands(HIVE))
    order = json.loads(fields["order"])
    assert (order["action"], order["execution_id"]) == ("retire", remote.agent.execution_id)


def test_a_failed_retire_is_ordered_again(remote):
    from scripts.hive.orders import Consumer, Orders
    from scripts.swarm_v2.runtime.base import LOCAL, Outcome, Status

    _publish(remote)
    runtime, agent = remote.runtime, remote.agent
    runtime.retire(agent)
    hive = Mock()
    hive.terminate.return_value = Outcome("terminate", Status.REFUSED, LOCAL, detail="process held")
    Consumer(remote.redis, HIVE, hive).poll_commands()
    assert runtime.retire(agent) is False
    assert "process held" in runtime.refusal(agent)["refusal"]
    assert runtime.retire(agent) is False
    assert len(remote.redis.xrange(Orders(remote.redis).commands(HIVE))) == 1


def test_a_nudge_reaches_the_remote_pane(remote, monkeypatch):
    from scripts.hive.orders import Consumer
    from scripts.swarm.delivery import marked
    from scripts.swarm.runtime import HerdrRuntime
    from scripts.swarm_v2.runtime.local import LocalHerdrRuntime

    _publish(remote)
    assert remote.runtime.nudge(remote.agent, "check your task") is None
    typed = []
    monkeypatch.setenv("SWARM_HIVE_ID", HIVE)
    hive = LocalHerdrRuntime(HerdrRuntime(herdr=lambda args: typed.append(args) or {}))
    assert Consumer(remote.redis, HIVE, hive).poll_commands() == 1
    assert typed == [["agent", "prompt", PANE, marked("check your task")]]


def test_a_nudge_to_a_lost_hive_is_refused(remote):
    from scripts.hive.orders import Orders

    assert "remote" in remote.runtime.nudge(remote.agent, "check your task")
    assert remote.redis.xrange(Orders(remote.redis).commands(HIVE)) == []


def test_pane_naming_and_stray_reaping_are_hive_orders(remote):
    from scripts.hive.orders import Consumer, Orders

    _publish(remote)
    runtime, agent = remote.runtime, remote.agent
    assert runtime.name_pane(agent) is False
    assert runtime.reap_name(agent.name) is False
    assert runtime.reap_name("engineer@nobody-0001") is True
    actions = [json.loads(f["order"])["action"] for _, f in remote.redis.xrange(Orders(remote.redis).commands(HIVE))]
    assert actions == ["name", "reap"]
    hive = Mock()
    hive.herdr.name_pane.return_value = True
    hive.herdr.reap_name.return_value = True
    assert Consumer(remote.redis, HIVE, hive).poll_commands() == 2
    assert hive.herdr.name_pane.call_args.args[0].execution_id == agent.execution_id
    hive.herdr.reap_name.assert_called_once_with(agent.name)
    assert runtime.name_pane(agent) is True
    assert runtime.reap_name(agent.name) is True
    assert not remote.redis.exists(Orders(remote.redis).command_receipt(HIVE, f"reap:{agent.name}"))
    assert runtime.reap_name(agent.name) is False
    assert len(remote.redis.xrange(Orders(remote.redis).commands(HIVE))) == 1


def test_a_hive_refuses_a_command_for_an_execution_it_does_not_run(remote):
    from scripts.hive.orders import Ack, CommandOrder, Consumer, Orders

    orders = Orders(remote.redis)
    stale = CommandOrder(
        "nudge:stale", "nudge", "fixture", remote.agent.name, remote.agent.execution_id, remote.agent.generation + 1
    )
    foreign = replace(stale, id="nudge:foreign", generation=remote.agent.generation)
    missing = replace(stale, id="nudge:missing", execution_id="nothing")
    for order in (stale, foreign, missing):
        assert orders.command(HIVE if order is not foreign else "elsewhere", order) is None
    hive = Mock()
    Consumer(remote.redis, HIVE, hive).poll_commands()
    Consumer(remote.redis, "elsewhere", hive).poll_commands()
    for order, at in ((stale, HIVE), (foreign, "elsewhere"), (missing, HIVE)):
        ack = orders.command(at, order)
        assert ack == Ack("failed", {}, "identity")
    hive.command.assert_not_called()


def test_local_targets_keep_the_local_path(remote, monkeypatch):
    from scripts.swarm.store import AgentRecord

    local = AgentRecord("engineer@local-0001", "eng", "task", pane_id="w9:p9")
    calls = []
    remote.runtime.herdr = lambda args: calls.append(args) or {"agents": []}
    assert remote.runtime.nudge(local, "hello") is None
    assert calls[0][:3] == ["agent", "prompt", "w9:p9"]
    assert remote.runtime.observe(local).state == "unknown"
    remote.runtime.hive = None
    _publish(remote)
    assert remote.agent.name not in remote.runtime.live_names()
    assert remote.runtime.observe(remote.agent).state == "unknown"


def _hive_side(monkeypatch, namespace, table):
    from scripts.swarm.runtime import HerdrRuntime
    from scripts.swarm_v2.runtime.local import LocalHerdrRuntime

    monkeypatch.setenv("SWARM_HIVE_ID", HIVE)
    ended = []
    herdr = HerdrRuntime(herdr=lambda args: {})
    herdr.end = lambda name, pid, homes, start: (
        ended.append((name, pid, start)) or SimpleNamespace(refusal="", process=pid)
    )
    return LocalHerdrRuntime(herdr, namespace=lambda: namespace, table=lambda: table), ended


def test_the_hive_retires_the_recorded_process_identity(remote, monkeypatch):
    from scripts.hive.orders import Consumer

    _publish(remote)
    runtime, agent = remote.runtime, remote.agent
    assert runtime.retire(agent) is False
    hive, ended = _hive_side(monkeypatch, "theirs", {12: SimpleNamespace(pid=12, start_time=34)})
    Consumer(remote.redis, HIVE, hive).poll_commands()
    assert ended == [(agent.name, 12, 34)]
    monkeypatch.setenv("SWARM_HIVE_ID", "controller")
    assert runtime.retire(agent) is True


def test_the_hive_refuses_a_retire_outside_the_recorded_namespace(remote, monkeypatch):
    from scripts.hive.orders import Consumer

    _publish(remote)
    runtime, agent = remote.runtime, remote.agent
    runtime.retire(agent)
    hive, ended = _hive_side(monkeypatch, "another", {12: SimpleNamespace(pid=12, start_time=34)})
    Consumer(remote.redis, HIVE, hive).poll_commands()
    assert ended == []
    monkeypatch.setenv("SWARM_HIVE_ID", "controller")
    assert runtime.retire(agent) is False
    assert "another PID namespace" in runtime.refusal(agent)["refusal"]


def test_a_runtime_without_hive_reports_never_acts_on_a_remote_worker_locally(remote):
    calls = []
    runtime, agent = remote.runtime, remote.agent
    runtime.hive = None
    runtime.herdr = lambda args: calls.append(args) or {}
    _publish(remote)
    assert runtime.retire(agent) is False
    assert runtime.refusals[agent.name] == {
        "process": 0,
        "refusal": f"no hive reports reach this runtime for hive {HIVE}",
    }
    assert runtime.nudge(agent, "hello") == f"hive {HIVE} is not reporting"
    assert runtime.name_pane(agent) is False
    assert runtime.observe(agent).state == "unknown"
    assert runtime.reported(agent) is False
    assert runtime.bindings([agent]) == {}
    assert calls == []


def test_a_failing_command_is_acknowledged_and_never_blocks_the_stream(remote):
    from scripts.hive.orders import Consumer, Orders

    _publish(remote)
    remote.runtime.retire(remote.agent)
    remote.runtime.nudge(remote.agent, "after")
    hive = Mock()
    hive.terminate.side_effect = RuntimeError("boom")
    assert Consumer(remote.redis, HIVE, hive).poll_commands() == 2
    assert remote.redis.xrange(Orders(remote.redis).commands(HIVE)) == []
    hive.command.assert_called_once()
    assert remote.runtime.retire(remote.agent) is False
    assert "error: RuntimeError" in remote.runtime.refusal(remote.agent)["refusal"]


def test_a_local_rebind_never_takes_a_remote_pane(remote):
    from scripts.swarm import tick
    from scripts.swarm.store import AgentRecord

    local = AgentRecord("engineer@local-0001", "eng", "task", conversation_id=CONVERSATION)
    runtime = Mock()
    runtime.conversations.return_value = {f"{HIVE}/{PANE}": CONVERSATION}
    assert tick._rebind("fixture", remote.store, runtime, local, 99) is None
    runtime.conversations.return_value = {"w9:p9": CONVERSATION}
    assert tick._rebind("fixture", remote.store, runtime, local, 99).pane_id == "w9:p9"


def test_a_lost_hive_keeps_its_workers_conversation_ids(remote):
    from scripts.swarm import tick

    agent = replace(remote.agent, conversation_id=CONVERSATION)
    remote.store.put_agent("fixture", agent)
    remote.runtime.herdr = lambda args: {"agents": []}
    tick._conversations("fixture", remote.store, remote.runtime)
    assert remote.store.agents("fixture")[0].conversation_id == CONVERSATION
    _publish(remote)
    report = json.loads(remote.redis.get(remote.reports.key(HIVE)))
    report["panes"] = {PANE: "resumed"}
    remote.redis.set(remote.reports.key(HIVE), json.dumps(report))
    tick._conversations("fixture", remote.store, remote.runtime)
    assert remote.store.agents("fixture")[0].conversation_id == "resumed"


def _retire_order(agent):
    from scripts.hive.orders import CommandOrder

    return CommandOrder(f"retire:{agent.execution_id}:{agent.generation}", "retire")


def _sent(redis):
    from scripts.hive.orders import Orders

    return [json.loads(fields["order"]) for _, fields in redis.xrange(Orders(redis).commands(HIVE))]


def test_report_and_receipt_keys_name_their_hive(remote):
    from scripts.hive import registry
    from scripts.hive.orders import Orders

    assert remote.reports.key(HIVE) == f"{registry.key(HIVE)}:executions"
    assert Orders(remote.redis).command_receipt(HIVE, "x") == f"{registry.key(HIVE)}:command:x"
    assert remote.runtime.hive.holder("engineer@nobody-0001") == ""
    assert remote.reports.host(replace(remote.agent, runtime_target={})) == ""


def test_an_execution_missing_from_a_live_report_reads_empty(remote):
    _publish(remote)
    assert remote.runtime.hive.execution(replace(remote.agent, execution_id="other")) == {}


def test_command_orders_carry_their_exact_identity(remote):
    _publish(remote)
    runtime, agent = remote.runtime, remote.agent
    waiting = "{} ordered on hive " + HIVE + ", waiting for its acknowledgement"
    runtime.retire(agent)
    assert runtime.refusals[agent.name] == {"process": 0, "refusal": waiting.format("retire")}
    runtime.name_pane(agent)
    assert runtime.refusals[agent.name] == {"process": 0, "refusal": waiting.format("name")}
    runtime.reap_name(agent.name)
    assert runtime.refusals[agent.name] == {"process": 0, "refusal": waiting.format("reap")}
    runtime.nudge(agent, "one")
    runtime.nudge(agent, "two")
    retire, name, reap, first, second = _sent(remote.redis)
    base = {
        "slug": "fixture",
        "name": agent.name,
        "execution_id": agent.execution_id,
        "generation": agent.generation,
        "text": "",
    }
    identity = f"{agent.execution_id}:{agent.generation}"
    assert retire == {**base, "id": f"retire:{identity}", "action": "retire"}
    assert name == {**base, "id": f"name:{identity}", "action": "name"}
    assert reap == {
        "id": f"reap:{agent.name}",
        "action": "reap",
        "slug": "",
        "name": agent.name,
        "execution_id": "",
        "generation": 0,
        "text": "",
    }
    assert first["id"].startswith(f"nudge:{identity}:")
    assert first["id"] != second["id"]
    assert (first["text"], second["text"]) == ("one", "two")


def test_a_refused_retire_names_the_hive_and_its_reason(remote):
    from scripts.hive.orders import Ack, Consumer, Orders
    from scripts.swarm_v2.runtime.base import LOCAL, Outcome, Status

    _publish(remote)
    runtime, agent = remote.runtime, remote.agent
    runtime.retire(agent)
    hive = Mock()
    hive.terminate.return_value = Outcome("terminate", Status.REFUSED, LOCAL, detail="process held")
    Consumer(remote.redis, HIVE, hive).poll_commands()
    assert Orders(remote.redis).command(HIVE, _retire_order(agent)) == Ack(
        "failed", agent.runtime_target, "process held"
    )
    assert runtime.retire(agent) is False
    assert runtime.refusals[agent.name] == {"process": 0, "refusal": f"hive {HIVE} refused retire: process held"}


def test_the_hive_retires_with_the_task_scratch_homes(remote, monkeypatch):
    from scripts.hive.orders import Consumer
    from scripts.swarm import reaper
    from scripts.swarm_v2.runtime.base import LOCAL, Outcome, Status

    _publish(remote)
    asked = []
    monkeypatch.setattr(reaper, "scratch_homes", lambda slug, task: asked.append((slug, task)) or ["/scratch/home"])
    remote.runtime.retire(remote.agent)
    hive = Mock()
    hive.terminate.return_value = Outcome("terminate", Status.OK, LOCAL)
    Consumer(remote.redis, HIVE, hive).poll_commands()
    assert asked == [("fixture", "task")]
    assert hive.terminate.call_args.args[1] == ("/scratch/home",)


def test_hive_command_acknowledgements_are_exact(remote):
    from scripts.hive.orders import Ack, CommandOrder, Consumer, Orders

    agent, orders = remote.agent, Orders(remote.redis)
    unknown = CommandOrder("explode", "explode", "fixture", agent.name, agent.execution_id, agent.generation)
    named = CommandOrder("name", "name", "fixture", agent.name, agent.execution_id, agent.generation)
    reaped = CommandOrder("reap", "reap", name=agent.name)
    failing = CommandOrder("fail", "retire", "fixture", agent.name, agent.execution_id, agent.generation)
    for order in (unknown, named, reaped, failing):
        orders.command(HIVE, order)
    hive = Mock()
    hive.herdr.name_pane.return_value = False
    hive.herdr.reap_name.return_value = True
    hive.terminate.side_effect = RuntimeError("boom")
    Consumer(remote.redis, HIVE, hive).poll_commands()
    assert orders.command(HIVE, unknown) == Ack("failed", {}, "unknown_action")
    assert orders.command(HIVE, named) == Ack("failed", {}, "refused")
    assert orders.command(HIVE, reaped) == Ack("done", {})
    assert orders.command(HIVE, failing) == Ack("failed", {}, "error: RuntimeError")


def test_the_hive_reads_commands_in_batches_and_skips_bad_and_answered_entries(remote):
    from dataclasses import asdict

    from scripts.hive.orders import CommandOrder, Consumer, Orders

    orders = Orders(remote.redis)
    stream = orders.commands(HIVE)
    remote.redis.xadd(stream, {"order": "not json"})
    for index in range(33):
        orders.command(HIVE, CommandOrder(f"reap:{index}", "reap", name=str(index)))
    hive = Mock()
    hive.herdr.reap_name.return_value = True
    consumer = Consumer(remote.redis, HIVE, hive)
    assert consumer.poll_commands() == 32
    assert hive.herdr.reap_name.call_count == 31
    assert len(remote.redis.xrange(stream)) == 2
    assert consumer.poll_commands() == 2
    assert hive.herdr.reap_name.call_count == 33
    remote.redis.xadd(stream, {"order": json.dumps(asdict(CommandOrder("reap:0", "reap", name="0")))})
    assert consumer.poll_commands() == 1
    assert hive.herdr.reap_name.call_count == 33
    assert remote.redis.xrange(stream) == []


def test_remote_bindings_drop_the_rebound_process_and_cover_every_worker(remote):
    _publish(remote)
    report = json.loads(remote.redis.get(remote.reports.key(HIVE)))
    report["executions"][remote.agent.execution_id]["binding"] = {**BINDING, "rebound": 77}
    remote.redis.set(remote.reports.key(HIVE), json.dumps(report))
    stale = replace(remote.agent, name="engineer@stale-0001", generation=remote.agent.generation + 1)
    assert remote.runtime.bindings([stale, remote.agent]) == {remote.agent.name: BINDING}


def test_a_runtime_without_hive_reports_reaps_local_strays_by_name(remote):
    remote.runtime.hive = None
    remote.runtime.reap = lambda pids: SimpleNamespace(refusal="")
    assert remote.runtime.reap_name("engineer@nobody-0001") is True


def test_the_hive_controller_reads_hive_reports_from_its_store(remote):
    from scripts.swarm import lease
    from scripts.swarm_v2.runtime.routed import hive_controller, routed

    held = lease.acquire(remote.store, "fixture", "controller")
    remote.runtime.hive = None
    hive_controller(routed(environ={}, herdr=remote.runtime), remote.store, "fixture", held)
    assert remote.runtime.hive.redis is remote.redis
