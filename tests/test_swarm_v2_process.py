from dataclasses import replace

import pytest

from hooks.proc import Process
from scripts.swarm import reaper
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError
from scripts.swarm.tick import tick
from scripts.swarm_v2.runtime import process
from scripts.swarm_v2.runtime.base import LOCAL, Outcome, RuntimeRouter, Status, Unqualified, legacy
from scripts.swarm_v2.runtime.local import LocalHerdrRuntime
from scripts.swarm_v2.runtime.routed import RoutedRuntime
from tests.swarm.test_tick import FakeLedger
from tests.swarm.test_tick import FakeRuntime as TickRuntime
from tests.test_swarm_v2_runtime import REMOTE, herdr_runtime, local_fake, remote_fake

ANTON = "boot-anton/pid:[4026531836]"
WORKER = "boot-aws-worker/pid:[4026532001]"
PID, STARTED = 4321, 777
NAME = "engineer@a1b2c3-0001"


def proc(pid=PID, start=STARTED):
    return Process(pid, 1, pid, pid, start, "S", "claude", ("claude",))


def record(namespace=ANTON, pid=PID, start=STARTED, execution="exe-1", backend=LOCAL, **target):
    runtime_target = {"process_namespace": namespace, "pid": pid, "pid_start": start, **target}
    return AgentRecord(
        NAME,
        "eng",
        "t1",
        pane_id="w1:p1",
        execution_id=execution,
        generation=1 if execution else 0,
        runtime_backend=backend,
        runtime_target={k: v for k, v in runtime_target.items() if v is not None},
    )


def test_the_local_namespace_is_the_boot_id_and_the_pid_namespace_link(tmp_path):
    boot, link = tmp_path / "boot_id", tmp_path / "pid"
    boot.write_text("boot-anton\n")
    link.symlink_to("pid:[4026531836]")
    assert process.local_namespace(boot, link) == ANTON


@pytest.mark.parametrize("missing", ["boot", "link"])
def test_an_unreadable_namespace_is_empty_rather_than_guessed(tmp_path, missing):
    boot, link = tmp_path / "boot_id", tmp_path / "pid"
    if missing != "boot":
        boot.write_text("boot-anton\n")
    if missing != "link":
        link.symlink_to("pid:[4026531836]")
    assert process.local_namespace(boot, link) == ""


def test_the_real_local_namespace_names_this_boot_and_pid_namespace():
    assert process.local_namespace().startswith(process.BOOT_ID.read_text().strip() + "/pid:[")


def test_a_matching_pid_and_start_time_in_the_owning_namespace_resolves_to_its_process():
    assert process.resolve(record(), ANTON, {PID: proc()}) == PID


def test_a_colliding_pid_in_a_foreign_namespace_is_refused():
    assert process.resolve(record(namespace=WORKER), ANTON, {PID: proc()}) is Unqualified.FOREIGN_NAMESPACE


@pytest.mark.parametrize("table", [{PID: proc(start=STARTED + 1)}, {}], ids=["reused", "gone"])
def test_a_reused_or_gone_pid_resolves_to_no_process(table):
    assert process.resolve(record(), ANTON, table) is None


def test_a_record_without_execution_identity_is_refused_even_when_its_pid_matches():
    assert process.resolve(record(execution=""), ANTON, {PID: proc()}) is Unqualified.NO_EXECUTION


@pytest.mark.parametrize("field", ["namespace", "pid", "start"])
def test_a_target_missing_any_part_of_the_process_identity_is_refused(field):
    agent = record(**{field: None})
    assert process.resolve(agent, ANTON, {PID: proc()}) is Unqualified.NO_PROCESS


def test_an_unreadable_local_namespace_refuses():
    assert process.resolve(record(), "", {PID: proc()}) is Unqualified.NO_NAMESPACE


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, True),
        ({"runtime_target": {"pid": PID}}, False),
        ({"execution_id": "exe-1"}, False),
        ({"generation": 1}, False),
        ({"runtime_backend": REMOTE}, False),
    ],
)
def test_only_an_unqualified_local_record_is_legacy(changes, expected):
    assert legacy(replace(AgentRecord(NAME, "eng", "t1"), **changes)) is expected


def test_the_router_refuses_a_terminate_without_execution_identity_and_counts_it():
    local, remote = local_fake(), remote_fake()
    router = RuntimeRouter([local, remote])
    agent = record(execution="")
    outcome = router.terminate(agent)
    assert outcome == Outcome("terminate", Status.REFUSED, LOCAL, Unqualified.NO_EXECUTION, "no execution identity")
    assert (local.calls, remote.calls) == ([], [])
    assert router.unqualified_process_actions_rejected_total() == 1


def test_the_router_refuses_a_remote_terminate_without_execution_identity():
    local, remote = local_fake(), remote_fake()
    router = RuntimeRouter([local, remote])
    agent = AgentRecord(NAME, "eng", "t1", runtime_backend=REMOTE)
    assert router.terminate(agent).status is Status.REFUSED
    assert (local.calls, remote.calls) == ([], [])
    assert router.rejected == {(REMOTE, Unqualified.NO_EXECUTION): 1}


def test_the_router_passes_a_legacy_local_terminate_to_the_local_adapter():
    local, remote = local_fake(), remote_fake()
    router = RuntimeRouter([local, remote])
    assert router.terminate(AgentRecord(NAME, "eng", "t1")).ok
    assert local.calls == [("terminate", NAME)]
    assert router.unqualified_process_actions_rejected_total() == 0


def adapter(tmp_path, monkeypatch, table, namespace=ANTON):
    herdr, calls = herdr_runtime(tmp_path, monkeypatch, panes={})
    ended = []

    def end(name, pid, homes):
        ended.append((name, pid, tuple(homes)))
        return reaper.Outcome()

    herdr.end = end
    return LocalHerdrRuntime(herdr, namespace=lambda: namespace, table=lambda: dict(table)), calls, ended


def test_a_qualified_local_terminate_signals_only_the_verified_pid(tmp_path, monkeypatch):
    local, calls, ended = adapter(tmp_path, monkeypatch, {PID: proc()})
    router = RuntimeRouter([local, remote_fake()])
    assert router.terminate(record(), ("/scratch/t1",)) == Outcome("terminate", Status.OK, LOCAL)
    assert ended == [(NAME, PID, ("/scratch/t1",))]
    assert ["pane", "close", "w1:p1"] in calls


def test_a_reused_pid_is_never_signalled_but_the_pane_and_homes_are_retired(tmp_path, monkeypatch):
    local, calls, ended = adapter(tmp_path, monkeypatch, {PID: proc(start=STARTED + 9)})
    assert RuntimeRouter([local]).terminate(record(), ("/scratch/t1",)).ok
    assert ended == [(NAME, None, ("/scratch/t1",))]
    assert ["pane", "close", "w1:p1"] in calls


def test_a_foreign_namespace_terminate_signals_nothing_and_counts_one_rejection(tmp_path, monkeypatch):
    local, calls, ended = adapter(tmp_path, monkeypatch, {PID: proc()})
    router = RuntimeRouter([local])
    outcome = router.terminate(record(namespace=WORKER))
    assert (outcome.status, outcome.value, outcome.detail) == (
        Status.REFUSED,
        Unqualified.FOREIGN_NAMESPACE,
        "process belongs to another PID namespace",
    )
    assert WORKER not in repr(outcome)
    assert (ended, calls) == ([], [])
    assert router.rejected == {(LOCAL, Unqualified.FOREIGN_NAMESPACE): 1}


def test_a_legacy_local_record_keeps_the_recorded_launch_pid_path(tmp_path, monkeypatch):
    local, calls, ended = adapter(tmp_path, monkeypatch, {})
    agent = AgentRecord(NAME, "eng", "t1", pane_id="w1:p1", profile_decision={"validation": {"pid": 55}})
    assert RuntimeRouter([local]).terminate(agent).ok
    assert ended == [(NAME, 55, ())]


def test_anton_and_a_remote_worker_sharing_pid_4321_are_each_ended_only_by_their_owner(tmp_path, monkeypatch):
    local, calls, ended = adapter(tmp_path, monkeypatch, {PID: proc()})
    remote = remote_fake()
    router = RuntimeRouter([local, remote], REMOTE)
    on_worker = replace(router.spawn(request_for("engineer@a1b2c3-0002")).value, execution_id="exe-2")
    assert router.terminate(on_worker).ok
    assert ended == [] and on_worker.name not in remote.objects
    assert router.terminate(record()).ok
    assert ended == [(NAME, PID, ())]
    assert [call for call in remote.calls if call[0] == "terminate"] == [("terminate", on_worker.name)]


def request_for(name):
    from tests.test_swarm_v2_runtime import request

    return request(name, "t2")


def test_the_routed_runtime_reports_why_the_router_refused(tmp_path, monkeypatch):
    herdr, _ = herdr_runtime(tmp_path, monkeypatch)
    routed = RoutedRuntime(herdr, RuntimeRouter([LocalHerdrRuntime(herdr, namespace=lambda: ANTON)]))
    agent = record(execution="")
    assert routed.retire(agent) is False
    assert routed.refusal(agent) == {"process": 0, "refusal": "no execution identity"}


def test_the_routed_runtime_keeps_the_adapter_refusal_when_a_retire_fails(tmp_path, monkeypatch):
    herdr, _ = herdr_runtime(tmp_path, monkeypatch)
    herdr.end = lambda name, pid, homes: reaper.Outcome(process=PID, refusal="survived SIGKILL: 4321")
    routed = RoutedRuntime(herdr, RuntimeRouter([LocalHerdrRuntime(herdr)]))
    agent = AgentRecord(NAME, "eng", "t1")
    assert routed.retire(agent) is False
    assert routed.refusal(agent) == {"process": PID, "refusal": "survived SIGKILL: 4321"}


def test_a_local_target_may_carry_the_pid_start_time():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0))
    name = store.next_name("sw", "eng")
    agent = AgentRecord(name, "eng", "t1", seat="eng-1@sw", runtime_target=record().runtime_target)
    assert store.start_execution("sw", agent).runtime_target["pid_start"] == STARTED
    with pytest.raises(SwarmError, match="runtime PID start time must be a positive integer"):
        store.start_execution(
            "sw",
            replace(
                agent,
                name=store.next_name("sw", "eng"),
                seat="eng-2@sw",
                runtime_target={**agent.runtime_target, "pid_start": "777"},
            ),
        )


@pytest.fixture
def remote_swarm():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0))
    name = store.next_name("sw", "eng")
    target = {"pod_namespace": "workers", "pod_name": "worker-1"}
    agent = AgentRecord(
        name, "eng", "t1", seat="eng-1@sw", started_at=1_000, runtime_backend=REMOTE, runtime_target=target
    )
    agent = store.start_execution("sw", agent)
    store.claim("sw", "t1", name, 60_000)
    ledger = FakeLedger([{"id": "t1", "lane": "eng", "state": "claimed", "claimed_by": name}])
    return store, ledger, agent


@pytest.mark.parametrize("repeat", range(2))
def test_a_remote_agent_without_an_answer_is_suspect_kept_and_never_ended_locally(remote_swarm, repeat):
    store, ledger, agent = remote_swarm
    runtime = TickRuntime()
    runtime.live.add("unrelated-local")
    runtime.statuses[agent.name] = "unknown"
    late = 1_000 + 10 * 60_000
    tick("sw", store, ledger, runtime, now_ms=late)
    kept = {a.name: a for a in store.agents("sw")}[agent.name]
    assert kept.state == "suspect" and kept.execution_id == agent.execution_id
    assert (runtime.killed, runtime.homes, runtime.reaped) == ([], {}, [])
    assert ledger.rows["t1"]["claimed_by"] == agent.name and runtime.spawned == []
    runtime.statuses[agent.name] = "working"
    tick("sw", store, ledger, runtime, now_ms=late + 1)
    assert {a.name: a for a in store.agents("sw")}[agent.name].state == "working"
    assert runtime.killed == []


def test_the_launch_check_never_judges_a_remote_agent_from_the_local_process_table(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.terminate_agent.sessions", lambda: [])
    herdr, _ = herdr_runtime(tmp_path, monkeypatch)
    validated = {"validation": {"pid": PID}}
    remote = AgentRecord(NAME, "eng", "t1", runtime_backend=REMOTE, profile_decision=validated)
    local = replace(remote, name="engineer@a1b2c3-0002", runtime_backend=LOCAL)
    assert herdr.bindings([remote, local]) == {local.name: {"process": False}}
