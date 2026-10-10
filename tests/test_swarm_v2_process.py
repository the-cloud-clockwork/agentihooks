import faulthandler
import os
import signal
import subprocess
import threading
from contextlib import contextmanager, suppress
from dataclasses import replace

import pytest

from hooks.proc import Process, processes
from scripts.swarm import reaper
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError
from scripts.swarm.tick import LEASE_MS, tick
from scripts.swarm_v2.runtime import process
from scripts.swarm_v2.runtime.base import LOCAL, Outcome, RuntimeRouter, Status, Unqualified, legacy
from scripts.swarm_v2.runtime.local import LocalHerdrRuntime
from scripts.swarm_v2.runtime.routed import RoutedRuntime
from tests.swarm.test_tick import FakeLedger
from tests.swarm.test_tick import FakeRuntime as TickRuntime
from tests.test_swarm_v2_runtime import REMOTE, herdr_runtime, local_fake, remote_fake, request

pytestmark = pytest.mark.xdist_group("fakeredis")

ANTON = "boot-anton/pid:[4026531836]"
WORKER = "boot-aws-worker/pid:[4026532001]"
PID, STARTED = 4321, 777
NAME = "engineer@a1b2c3-0001"


def proc(pid=PID, start=STARTED):
    return Process(pid, 1, pid, pid, start, "S", "claude", ("claude",))


def record(namespace=ANTON, pid=PID, start=STARTED, execution="exe-1"):
    runtime_target = {"process_namespace": namespace, "pid": pid, "pid_start": start}
    return AgentRecord(
        NAME,
        "eng",
        "t1",
        pane_id="w1:p1",
        execution_id=execution,
        generation=1 if execution else 0,
        runtime_target={k: v for k, v in runtime_target.items() if v is not None},
    )


def children():
    rows = [row for row in processes().values() if row.state != "Z"]
    found, parents = set(), {os.getpid()}
    while parents:
        parents = {row.pid for row in rows if row.ppid in parents} - found
        found |= parents
    return found


@contextmanager
def bounded(what, seconds=30):
    finished = []

    def expired(signum, frame):
        if not finished:
            pytest.fail(f"{what} did not finish within {seconds} seconds")

    before = children()
    previous = signal.signal(signal.SIGALRM, expired)

    # Ends the worker, which xdist reports as a crash in this test, when SIGALRM cannot interrupt the wait.
    def abort():
        faulthandler.dump_traceback()
        os._exit(1)

    watchdog = threading.Timer(seconds + 10, abort)
    watchdog.start()
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    finally:
        finished.append(True)
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        left = children() - before
        for pid in left:
            with suppress(ProcessLookupError, ChildProcessError):
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
        watchdog.cancel()
    assert not left, f"{what} left child processes running"


def test_bounded_does_not_cancel_a_pending_faulthandler_dump(tmp_path):
    with (tmp_path / "dump").open("w+") as dump:
        faulthandler.dump_traceback_later(0.5, file=dump)
        try:
            with bounded("nothing"):
                pass
            threading.Event().wait(1)
        finally:
            faulthandler.cancel_dump_traceback_later()
        dump.seek(0)
        assert "Timeout" in dump.read()


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

    def end(name, pid, homes, start=0):
        ended.append((name, pid, tuple(homes), start))
        return reaper.Outcome()

    herdr.end = end
    return LocalHerdrRuntime(herdr, namespace=lambda: namespace, table=lambda: dict(table)), calls, ended


def test_a_qualified_local_terminate_signals_only_the_verified_pid_with_its_start_time(tmp_path, monkeypatch):
    local, calls, ended = adapter(tmp_path, monkeypatch, {PID: proc()})
    router = RuntimeRouter([local, remote_fake()])
    assert router.terminate(record(), ("/scratch/t1",)) == Outcome("terminate", Status.OK, LOCAL)
    assert ended == [(NAME, PID, ("/scratch/t1",), STARTED)]
    assert ["pane", "close", "w1:p1"] in calls


def test_the_recorded_pid_of_a_reused_process_is_never_signalled_but_pane_and_homes_retire(tmp_path, monkeypatch):
    local, calls, ended = adapter(tmp_path, monkeypatch, {PID: proc(start=STARTED + 9)})
    assert RuntimeRouter([local]).terminate(record(), ("/scratch/t1",)).ok
    assert ended == [(NAME, None, ("/scratch/t1",), STARTED)]
    assert ["pane", "close", "w1:p1"] in calls


def test_a_foreign_namespace_terminate_signals_nothing_and_counts_one_rejection(tmp_path, monkeypatch):
    local, calls, ended = adapter(tmp_path, monkeypatch, {PID: proc()})
    router = RuntimeRouter([local])
    outcome = router.terminate(record(namespace=WORKER))
    assert outcome == Outcome(
        "terminate", Status.REFUSED, LOCAL, Unqualified.FOREIGN_NAMESPACE, "process belongs to another PID namespace"
    )
    assert WORKER not in repr(outcome)
    assert (ended, calls) == ([], [])
    assert router.rejected == {(LOCAL, Unqualified.FOREIGN_NAMESPACE): 1}


def test_a_legacy_local_record_keeps_the_recorded_launch_pid_path(tmp_path, monkeypatch):
    local, calls, ended = adapter(tmp_path, monkeypatch, {})
    agent = AgentRecord(NAME, "eng", "t1", pane_id="w1:p1", profile_decision={"validation": {"pid": 55}})
    assert RuntimeRouter([local]).terminate(agent).ok
    assert ended == [(NAME, 55, (), 0)]


def on_worker(router):
    spawned = router.spawn(request("engineer@a1b2c3-0002", "t2")).value
    worker_process = {"process_namespace": WORKER, "pid": PID, "pid_start": STARTED}
    return replace(spawned, runtime_target={**spawned.runtime_target, **worker_process})


def test_anton_and_a_remote_worker_sharing_pid_4321_are_each_ended_only_by_their_owner(tmp_path, monkeypatch):
    local, calls, ended = adapter(tmp_path, monkeypatch, {PID: proc()})
    remote = remote_fake()
    router = RuntimeRouter([local, remote], REMOTE)
    worker = on_worker(router)
    assert router.terminate(worker).ok
    assert ended == [] and worker.name not in remote.objects
    assert router.terminate(record()).ok
    assert ended == [(NAME, PID, (), STARTED)]
    assert [call for call in remote.calls if call[0] == "terminate"] == [("terminate", worker.name)]
    assert router.unqualified_process_actions_rejected_total() == 0


def test_a_qualified_execution_started_by_the_store_terminates_through_the_router(tmp_path, monkeypatch):
    import fakeredis

    with bounded("the store started execution terminate"):
        store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
        store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0))
        seed = replace(record(execution=""), name=store.next_name("sw", "eng"), seat="eng-1@sw")
        started = store.start_execution("sw", seed)
        local, calls, ended = adapter(tmp_path, monkeypatch, {PID: proc()})
        assert RuntimeRouter([local]).terminate(store.execution("sw", started.execution_id)).ok
        assert ended == [(started.name, PID, (), STARTED)]


def test_a_store_call_blocked_on_its_server_fails_within_its_bound():
    import fakeredis

    server = fakeredis.FakeServer()
    store = RedisStore(fakeredis.FakeRedis(server=server, decode_responses=True))
    server.lock.acquire()
    release = threading.Timer(5, server.lock.release)
    release.start()
    try:
        with pytest.raises(pytest.fail.Exception, match="^swarm create did not finish within 0.5 seconds$"):
            with bounded("swarm create", 0.5):
                store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0))
    finally:
        release.cancel()
        if server.lock.locked():
            server.lock.release()


def test_the_bound_refuses_a_child_process_that_outlives_the_test():
    child = None
    try:
        with pytest.raises(AssertionError, match="a sleeper left child processes running"):
            with bounded("a sleeper"):
                child = subprocess.Popen(["sleep", "30"])
        assert child.pid not in processes()
    finally:
        if child is not None:
            child.kill()
            child.wait()


def test_the_routed_runtime_reports_why_the_router_refused(tmp_path, monkeypatch):
    herdr, _ = herdr_runtime(tmp_path, monkeypatch)
    routed = RoutedRuntime(herdr, RuntimeRouter([LocalHerdrRuntime(herdr, namespace=lambda: ANTON)]))
    agent = record(execution="")
    assert routed.retire(agent) is False
    assert routed.refusal(agent) == {"process": 0, "refusal": "no execution identity"}


def test_the_routed_runtime_keeps_the_adapter_refusal_when_a_retire_fails(tmp_path, monkeypatch):
    herdr, _ = herdr_runtime(tmp_path, monkeypatch)
    herdr.end = lambda name, pid, homes, start=0: reaper.Outcome(process=PID, refusal="survived SIGKILL: 4321")
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


def launch_store():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0))
    return store


def launched(store, pid=PID):
    decision = {"validation": {"pid": pid}} if pid is not None else {}
    return AgentRecord(store.next_name("sw", "eng"), "eng", "t1", seat="eng-1@sw", profile_decision=decision)


def launcher(namespace=ANTON, table=None):
    rows = {PID: proc()} if table is None else table
    return LocalHerdrRuntime(None, namespace=lambda: namespace, table=lambda: rows)


@pytest.mark.parametrize("pid", [PID, 1])
def test_a_local_launch_stores_its_process_namespace_number_and_start_time(pid):
    store = launch_store()
    table = {pid: proc(pid=pid)}
    started = launcher(table=table).admit(store, "sw", launched(store, pid))
    stored = store.execution("sw", started.execution_id)
    assert stored.runtime_backend == LOCAL
    assert stored.runtime_target == {"process_namespace": ANTON, "pid": pid, "pid_start": STARTED}
    assert process.resolve(stored, ANTON, table) == pid


def test_a_local_relaunch_replaces_the_execution_it_names_on_the_seat():
    store = launch_store()
    first = launcher().admit(store, "sw", launched(store))
    second = launcher().admit(store, "sw", launched(store), first.execution_id)
    assert (second.seat, second.generation) == ("eng-1@sw", 2)
    assert store.execution_occupants("sw")["eng-1@sw"].execution_id == second.execution_id


@pytest.mark.parametrize(
    ("namespace", "table", "pid", "missing"),
    [
        ("", None, PID, "process namespace"),
        (ANTON, {}, PID, "start time"),
        (ANTON, {PID: proc(start=0)}, PID, "start time"),
        (ANTON, None, None, "process number and start time"),
        (ANTON, None, True, "process number and start time"),
        (ANTON, None, float(PID), "process number and start time"),
        (ANTON, {-PID: proc(pid=-PID)}, -PID, "process number and start time"),
        ("", {}, None, "process namespace, process number and start time"),
    ],
)
def test_a_local_launch_that_cannot_read_its_process_identity_is_refused_and_leaves_no_record(
    namespace, table, pid, missing
):
    store = launch_store()
    with pytest.raises(SwarmError) as refused:
        launcher(namespace, table).admit(store, "sw", launched(store, pid))
    assert str(refused.value) == f"local launch refused: its {missing} could not be read"
    assert store.execution_registry.records("sw") == []
    assert store.agents("sw") == []
    assert store.execution_identity_conflicts_total("sw") == 0


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
    assert store.claimant("sw", "t1") == agent.name and store.redis.pttl(store.key("sw", "claim", "t1")) > 60_000
    assert store.redis.pttl(store.key("sw", "claim", "t1")) <= LEASE_MS
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


def test_retire_process_hands_the_reaper_the_verified_start_time_and_legacy_retire_none(tmp_path, monkeypatch):
    herdr, _ = herdr_runtime(tmp_path, monkeypatch)
    assert isinstance(herdr, HerdrRuntime)
    seen = []
    herdr.end = lambda name, pid, homes, start=-1: seen.append((name, pid, homes, start)) or reaper.Outcome()
    assert herdr.retire_process(AgentRecord(NAME, "eng", "t1"), PID, ("/scratch/t1",), STARTED)
    assert herdr.retire(AgentRecord(NAME, "eng", "t1", profile_decision={"validation": {"pid": 55}}))
    assert seen == [(NAME, PID, ["/scratch/t1"], STARTED), (NAME, 55, [], 0)]


def test_each_unqualified_terminate_is_counted():
    router = RuntimeRouter([local_fake()])
    router.terminate(record(execution=""))
    router.terminate(record(execution=""))
    assert router.rejected == {(LOCAL, Unqualified.NO_EXECUTION): 2}
    assert router.unqualified_process_actions_rejected_total() == 2


def test_an_operation_on_an_unregistered_backend_names_that_operation():
    stray = AgentRecord(NAME, "eng", "t1", runtime_backend="ssh")
    outcome = RuntimeRouter([local_fake()]).observe(stray)
    assert outcome == Outcome("observe", Status.UNAVAILABLE, "ssh", detail="no runtime registered for ssh")


def test_a_successful_retire_clears_the_routed_refusal(tmp_path, monkeypatch):
    herdr, _ = herdr_runtime(tmp_path, monkeypatch)
    routed = RoutedRuntime(herdr, RuntimeRouter([LocalHerdrRuntime(herdr)]))
    refused, legacy_agent = record(execution=""), AgentRecord(NAME, "eng", "t1")
    assert routed.retire(refused) is False
    assert routed.retire(legacy_agent) is True
    assert routed.refusal(legacy_agent) == {"process": 0, "refusal": "unknown"}


def test_the_reap_keeps_earlier_actions_when_a_remote_agent_turns_suspect(remote_swarm):
    store, ledger, agent = remote_swarm
    local_name = store.next_name("sw", "ci")
    assert local_name < agent.name
    store.put_agent("sw", AgentRecord(local_name, "ci", "t0", started_at=1_000))
    ledger.rows["t0"] = {"id": "t0", "lane": "ci", "state": "done", "claimed_by": local_name, "out_of_scope": False}
    runtime = TickRuntime()
    runtime.live.add(local_name)
    runtime.statuses[agent.name] = "unknown"
    actions = tick("sw", store, ledger, runtime, now_ms=2_000)
    assert f"retired {local_name}" in actions
    assert f"suspect {agent.name}: its runtime did not answer" in actions


def test_an_idle_remote_agent_is_nudged_then_retired_through_its_runtime(remote_swarm):
    from scripts.gates import log
    from scripts.swarm.tick import IDLE_KILL_TICKS, IDLE_NUDGE_TICKS

    store, ledger, agent = remote_swarm
    store.update("sw", state="paused")
    runtime = TickRuntime()
    runtime.statuses[agent.name] = "idle"
    for n in range(IDLE_KILL_TICKS):
        tick("sw", store, ledger, runtime, now_ms=2_000 + n)
        if n + 1 == IDLE_NUDGE_TICKS:
            assert runtime.nudged == [agent.name]
    assert [row["at"] for row in log.recent("sw")][:2] == [2_000, 2_001]
    assert agent.name in runtime.homes and ledger.rows["t1"]["state"] == "open"


def test_each_acceptance_case_passes():
    from tests.sv2_run02_cases import case_a, case_b, case_c

    assert [case()["passed"] for case in (case_a, case_b, case_c)] == [True, True, True]
