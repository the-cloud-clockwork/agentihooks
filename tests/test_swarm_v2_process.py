import faulthandler
import os
import signal
import subprocess
import sys
import threading
import time
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
    outcome = router.terminate(record(namespace="boot-anton/pid:[4026539999]"))
    assert outcome == Outcome(
        "terminate", Status.REFUSED, LOCAL, Unqualified.FOREIGN_NAMESPACE, "process belongs to another PID namespace"
    )
    assert WORKER not in repr(outcome)
    assert (ended, calls) == ([], [])
    assert router.rejected == {(LOCAL, Unqualified.FOREIGN_NAMESPACE): 1}


@pytest.mark.parametrize("pid_namespace", ["pid:[4026531836]", "pid:[4026539999]"])
def test_a_local_record_from_a_previous_boot_retires_as_ended_without_signalling(tmp_path, monkeypatch, pid_namespace):
    local, calls, ended = adapter(tmp_path, monkeypatch, {PID: proc()})
    router = RuntimeRouter([local])
    agent = record(namespace=f"boot-before/{pid_namespace}")
    monkeypatch.setattr(local, "table", lambda: pytest.fail("previous boot retirement read the current process table"))
    assert router.terminate(agent, ("/scratch/t1",)) == Outcome(
        "terminate", Status.OK, LOCAL, detail="its process ended with a previous boot"
    )
    assert (ended, calls) == ([], [])
    assert router.rejected == {}


def test_an_old_boot_record_retires_its_reopened_conversation_process(tmp_path, monkeypatch):
    from scripts.terminate_agent import Session

    local, calls, ended = adapter(tmp_path, monkeypatch, {PID: proc()})
    agent = replace(record(namespace=WORKER), harness="codex", conversation_id="restored")
    current = proc(pid=PID + 1, start=STARTED + 1)
    unrelated = Session("another", "codex", NAME, proc(pid=PID + 2), "", "alive")
    reopened = Session("restored", "codex", NAME, current, "", "alive")
    monkeypatch.setattr("scripts.terminate_agent.sessions", lambda: [unrelated, reopened])
    router = RuntimeRouter([local])
    assert router.terminate(agent, ("/scratch/t1",)) == Outcome("terminate", Status.OK, LOCAL)
    assert ended == [(NAME, PID + 1, ("/scratch/t1",), STARTED + 1)]
    assert ["pane", "close", "w1:p1"] in calls
    assert router.rejected == {}


@pytest.mark.parametrize(
    ("field", "value"),
    [("name", "another"), ("session_id", "another"), ("target", "claude"), ("status", "dead")],
)
def test_previous_boot_retirement_does_not_signal_another_conversation_process(tmp_path, monkeypatch, field, value):
    from scripts.terminate_agent import Session

    local, calls, ended = adapter(tmp_path, monkeypatch, {PID: proc()})
    agent = replace(record(namespace=WORKER), harness="codex", conversation_id="restored")
    candidate = replace(Session("restored", "codex", NAME, proc(pid=PID + 1), "", "alive"), **{field: value})
    monkeypatch.setattr("scripts.terminate_agent.sessions", lambda: [candidate])
    assert RuntimeRouter([local]).terminate(agent) == Outcome(
        "terminate", Status.OK, LOCAL, detail="its process ended with a previous boot"
    )
    assert (ended, calls) == ([], [])


def test_previous_boot_retirement_without_a_conversation_never_selects_a_named_process(tmp_path, monkeypatch):
    local, calls, ended = adapter(tmp_path, monkeypatch, {PID: proc()})
    monkeypatch.setattr("scripts.terminate_agent.sessions", lambda: pytest.fail("no recorded conversation to recover"))
    assert RuntimeRouter([local]).terminate(record(namespace=WORKER)) == Outcome(
        "terminate", Status.OK, LOCAL, detail="its process ended with a previous boot"
    )
    assert (ended, calls) == ([], [])


def test_reopened_process_retirement_keeps_the_reaper_refusal(tmp_path, monkeypatch):
    from scripts.terminate_agent import Session

    local, calls, ended = adapter(tmp_path, monkeypatch, {PID: proc()})
    agent = replace(record(namespace=WORKER), harness="codex", conversation_id="restored")
    reopened = Session("restored", "codex", NAME, proc(pid=PID + 1), "", "alive")
    monkeypatch.setattr("scripts.terminate_agent.sessions", lambda: [reopened])
    local.herdr.end = lambda *args: reaper.Outcome(process=PID + 1, refusal="process identity changed")
    assert RuntimeRouter([local]).terminate(agent) == Outcome(
        "terminate",
        Status.REFUSED,
        LOCAL,
        {"process": PID + 1, "refusal": "process identity changed"},
        "process identity changed",
    )
    assert (ended, calls) == ([], [])


@pytest.mark.parametrize("host_id", ["", "hive-a"])
def test_a_same_boot_foreign_pid_namespace_stays_refused(tmp_path, monkeypatch, host_id):
    monkeypatch.setattr("scripts.swarm.commands.hive_id", lambda: "hive-a")
    local, calls, ended = adapter(tmp_path, monkeypatch, {PID: proc()})
    agent = record(namespace="boot-anton/pid:[4026539999]")
    if host_id:
        agent = replace(agent, runtime_target={**agent.runtime_target, "host_id": host_id})
    router = RuntimeRouter([local])
    assert router.terminate(agent) == Outcome(
        "terminate", Status.REFUSED, LOCAL, Unqualified.FOREIGN_NAMESPACE, "process belongs to another PID namespace"
    )
    assert (ended, calls) == ([], [])
    assert router.rejected == {(LOCAL, Unqualified.FOREIGN_NAMESPACE): 1}


@pytest.mark.parametrize("field", ["namespace", "pid", "start"])
def test_local_retirement_refuses_an_incomplete_process_identity(tmp_path, monkeypatch, field):
    local, calls, ended = adapter(tmp_path, monkeypatch, {PID: proc()})
    router = RuntimeRouter([local])
    assert router.terminate(record(**{field: None})) == Outcome(
        "terminate", Status.REFUSED, LOCAL, Unqualified.NO_PROCESS, "runtime target names no process"
    )
    assert (ended, calls) == ([], [])
    assert router.rejected == {(LOCAL, Unqualified.NO_PROCESS): 1}


def test_retirement_without_a_recorded_pid_ends_only_its_owned_pane(tmp_path, monkeypatch):
    from scripts import terminate_agent

    pane = {"pane_id": "w1:p1", "name": NAME, "agent_session": None}
    herdr, calls = herdr_runtime(tmp_path, monkeypatch, panes={"w1:p1": pane})
    herdr.end = reaper.retire
    environment = {
        **os.environ,
        "AGENTIHOOKS_AGENT_NAME": NAME,
        "HERDR_PANE_ID": "w1:p1",
        "HERDR_SOCKET_PATH": "/owned/herdr.sock",
    }
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/owned/herdr.sock")
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], env=environment, start_new_session=True
    )
    try:
        table = processes()
        session = terminate_agent.Session("", "claude", NAME, table[child.pid], "", "alive")
        monkeypatch.setattr(terminate_agent, "sessions", lambda: [session])
        local = LocalHerdrRuntime(herdr, namespace=lambda: ANTON, table=lambda: table)
        agent = replace(record(pid=None, start=None), state="finished")
        assert RuntimeRouter([local]).terminate(agent).ok
        assert child.wait(timeout=5) == -signal.SIGTERM
        assert ["pane", "close", "w1:p1"] in calls
        assert all(NAME not in call for call in calls)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


@pytest.mark.parametrize("owner", [None, "", "engineer@a1b2c3-0009"])
def test_a_live_agent_without_a_pid_or_owned_pane_is_refused(tmp_path, monkeypatch, owner):
    panes = {} if owner is None else {"w1:p1": {"pane_id": "w1:p1", "name": owner, "agent_session": {"value": "other"}}}
    herdr, calls = herdr_runtime(tmp_path, monkeypatch, panes=panes)
    monkeypatch.setattr(herdr, "live_names", lambda: {NAME})
    local = LocalHerdrRuntime(herdr, namespace=lambda: ANTON, table=lambda: {PID: proc()})
    outcome = RuntimeRouter([local]).terminate(record(pid=None, start=None))
    assert not outcome.ok
    assert outcome.detail == "no owned pane for unrecorded process"
    assert herdr.refusal(record(pid=None, start=None)) == {
        "process": 0,
        "refusal": "no owned pane for unrecorded process",
    }
    assert ["pane", "close", "w1:p1"] not in calls


def test_a_live_agent_with_no_pid_and_no_recorded_pane_retires_by_its_session_name(tmp_path, monkeypatch):
    from scripts import terminate_agent

    herdr, calls = herdr_runtime(tmp_path, monkeypatch, panes={})
    herdr.end = reaper.retire
    inherited = {key: value for key, value in os.environ.items() if not key.startswith("HERDR_")}
    environment = {**inherited, "AGENTIHOOKS_AGENT_NAME": NAME, "AGENTIHOOKS_SWARM_TASK": "t1"}
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], env=environment, start_new_session=True
    )
    try:
        table = processes()
        for _ in range(100):
            if child.pid in table and table[child.pid].pgid == child.pid:
                break
            time.sleep(0.05)
            table = processes()
        assert child.pid in table
        session = terminate_agent.Session("", "claude", NAME, table[child.pid], "", "alive")
        monkeypatch.setattr(terminate_agent, "sessions", lambda: [session])
        local = LocalHerdrRuntime(herdr, namespace=lambda: ANTON, table=lambda: table)
        agent = replace(record(pid=None, start=None), pane_id="", state="finished")
        assert RuntimeRouter([local]).terminate(agent).ok
        assert child.wait(timeout=5) == -signal.SIGTERM
        assert not any(call[:2] == ["pane", "close"] for call in calls)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


def test_named_retirement_passes_the_process_start_and_skips_a_vanished_recorded_pane(tmp_path, monkeypatch):
    herdr, calls = herdr_runtime(tmp_path, monkeypatch, panes={})
    ended = []
    herdr.end = lambda *args: ended.append(args) or reaper.Outcome()
    monkeypatch.setattr(herdr, "live_names", lambda: {NAME})
    monkeypatch.setattr("scripts.swarm.runtime._named_process", lambda agent: proc(start=999))
    agent = replace(record(pid=None, start=None), pane_id="w7:p7", state="finished")
    assert herdr.retire_pane(agent)
    assert ended == [(NAME, PID, [], 999)]
    assert ["pane", "close", "w7:p7"] not in calls


@pytest.mark.parametrize(
    ("recorded", "carried", "retired"),
    [
        ("", ("", "", "t1"), True),
        ("w7:p7", ("w7:p7", "/owned/herdr.sock", "t1"), True),
        ("w7:p7", ("w9:p9", "/owned/herdr.sock", "t1"), False),
        ("", ("w9:p9", "/other/herdr.sock", "t1"), False),
        ("", ("", "", "t2"), False),
        ("", ("", "", ""), False),
    ],
)
def test_named_retirement_ends_only_a_process_of_this_task_pane_and_server(
    tmp_path, monkeypatch, recorded, carried, retired
):
    from scripts import terminate_agent

    herdr, calls = herdr_runtime(tmp_path, monkeypatch, panes={})
    ended = []
    herdr.end = lambda *args: ended.append(args) or reaper.Outcome()
    monkeypatch.setattr(herdr, "live_names", lambda: {NAME})
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/owned/herdr.sock")
    session = terminate_agent.Session("", "claude", NAME, proc(), "", "alive")
    monkeypatch.setattr(terminate_agent, "sessions", lambda: [session])
    monkeypatch.setattr(terminate_agent, "validate", lambda *args, **kwargs: [])
    asked = []
    monkeypatch.setattr(terminate_agent, "agent_environ", lambda pid, keys: asked.append((pid, keys)) or carried)
    agent = replace(record(pid=None, start=None), pane_id=recorded, state="finished")
    assert herdr.retire_pane(agent) is retired
    assert asked == [(PID, ("HERDR_PANE_ID", "HERDR_SOCKET_PATH", "AGENTIHOOKS_SWARM_TASK"))]
    assert ended == ([(NAME, PID, [], STARTED)] if retired else [])
    if not retired:
        assert herdr.refusal(agent) == {"process": 0, "refusal": "no owned pane for unrecorded process"}


@pytest.mark.parametrize(("pane_id", "socket"), [("w9:p9", "/owned/herdr.sock"), ("w1:p1", "/other/herdr.sock")])
def test_unrecorded_retirement_refuses_a_named_process_on_another_pane_or_server(
    tmp_path, monkeypatch, pane_id, socket
):
    from scripts import terminate_agent

    pane = {"pane_id": "w1:p1", "name": NAME}
    herdr, calls = herdr_runtime(tmp_path, monkeypatch, panes={"w1:p1": pane})
    ended = []
    herdr.end = lambda *args: ended.append(args) or reaper.Outcome()
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/owned/herdr.sock")
    session = terminate_agent.Session("", "claude", NAME, proc(), "", "alive")
    monkeypatch.setattr(terminate_agent, "sessions", lambda: [session])
    monkeypatch.setattr(terminate_agent, "herdr_pane", lambda pid: (pane_id, socket))
    local = LocalHerdrRuntime(herdr, namespace=lambda: ANTON, table=lambda: {PID: proc()})
    assert not RuntimeRouter([local]).terminate(record(pid=None, start=None)).ok
    assert ended == []
    assert ["pane", "close", "w1:p1"] not in calls


@pytest.mark.parametrize("wrapped", [True, False])
@pytest.mark.parametrize("named", [True, False])
def test_unrecorded_retirement_uses_the_oldest_owned_process_with_its_start_and_scratch_homes(
    tmp_path, monkeypatch, wrapped, named
):
    from scripts import terminate_agent

    pane = {
        "pane_id": "w1:p1",
        "name": NAME if named else "",
        "agent_session": {"kind": "id", "value": "conversation"},
    }
    herdr, calls = herdr_runtime(tmp_path, monkeypatch, panes={"w1:p1": pane})
    original = herdr.herdr

    def lookup(args):
        result = original(args)
        return result["agent"] if not wrapped and args[:2] == ["agent", "get"] else result

    herdr.herdr = lookup
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/owned/herdr.sock")
    recent, oldest = proc(PID, 900), proc(PID + 1, 100)
    sessions = [terminate_agent.Session("", "claude", NAME, p, "", "alive") for p in (recent, oldest)]
    monkeypatch.setattr(terminate_agent, "sessions", lambda: sessions)
    monkeypatch.setattr(terminate_agent, "herdr_pane", lambda pid: ("w1:p1", "/owned/herdr.sock"))
    ended = []
    herdr.end = lambda *args: ended.append(args) or reaper.Outcome()
    local = LocalHerdrRuntime(herdr, namespace=lambda: ANTON, table=lambda: {})
    agent = replace(record(pid=None, start=None), conversation_id="conversation")
    assert RuntimeRouter([local]).terminate(agent, ("/scratch/task",)).ok
    assert ended == [(NAME, PID + 1, ["/scratch/task"], 100)]
    assert ["pane", "close", "w1:p1"] in calls


def test_unrecorded_retirement_reports_an_unavailable_pane_service(tmp_path, monkeypatch):
    herdr, calls = herdr_runtime(tmp_path, monkeypatch)

    def unavailable(args):
        calls.append(args)
        raise RuntimeError("pane service unavailable")

    herdr.herdr = unavailable
    local = LocalHerdrRuntime(herdr, namespace=lambda: ANTON, table=lambda: {})
    agent = record(pid=None, start=None)
    outcome = RuntimeRouter([local]).terminate(agent)
    assert not outcome.ok and outcome.detail == "pane service unavailable"
    assert herdr.refusal(agent) == {"process": 0, "refusal": "pane service unavailable"}
    assert calls == [["agent", "get", "w1:p1"]]


@pytest.mark.parametrize("present", [True, False])
def test_unrecorded_retirement_reaps_scratch_homes_when_the_process_is_already_gone(tmp_path, monkeypatch, present):
    from scripts import terminate_agent

    panes = {"w1:p1": {"pane_id": "w1:p1", "name": NAME}} if present else {}
    herdr, calls = herdr_runtime(tmp_path, monkeypatch, panes=panes)
    monkeypatch.setattr(terminate_agent, "sessions", lambda: [])
    ended = []
    herdr.end = lambda *args: ended.append(args) or reaper.Outcome()
    local = LocalHerdrRuntime(herdr, namespace=lambda: ANTON, table=lambda: {})
    assert RuntimeRouter([local]).terminate(record(pid=None, start=None), ("/scratch/task",)).ok
    assert ended == [(NAME, None, ["/scratch/task"], 0)]
    closed = [call for call in calls if call[:2] == ["pane", "close"]]
    assert closed == ([["pane", "close", "w1:p1"]] if present else [])


@pytest.mark.parametrize("conversation", ["", "recorded-conversation"])
def test_unrecorded_retirement_refuses_an_unnamed_pane_without_its_recorded_conversation(
    tmp_path, monkeypatch, conversation
):
    from scripts import terminate_agent

    pane = {"pane_id": "w1:p1", "name": "", "agent_session": None}
    herdr, calls = herdr_runtime(tmp_path, monkeypatch, panes={"w1:p1": pane})
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/owned/herdr.sock")
    session = terminate_agent.Session("", "claude", NAME, proc(), "", "alive")
    monkeypatch.setattr(terminate_agent, "sessions", lambda: [session])
    monkeypatch.setattr(terminate_agent, "herdr_pane", lambda pid: ("w1:p1", "/owned/herdr.sock"))
    ended = []
    herdr.end = lambda *args: ended.append(args) or reaper.Outcome()
    local = LocalHerdrRuntime(herdr, namespace=lambda: ANTON, table=lambda: {})
    agent = replace(record(pid=None, start=None), conversation_id=conversation)
    outcome = RuntimeRouter([local]).terminate(agent)
    assert not outcome.ok and outcome.detail == "no owned pane for unrecorded process"
    assert ended == []
    assert ["pane", "close", "w1:p1"] not in calls


@pytest.mark.parametrize(
    ("recorded", "current", "expected"),
    [
        ("old/pid:[1]", "new/pid:[1]", True),
        ("old/pid:[1]", "new/pid:[2]", True),
        ("same/pid:[1]", "same/pid:[2]", False),
        ("same/pid:[1]", "same/pid:[1]", False),
        ("", "new/pid:[1]", False),
        ("old/pid:[1]", "", False),
        ("old", "new/pid:[1]", False),
        ("old/pid:[1]", "new", False),
        ("/pid:[1]", "new/pid:[1]", False),
        ("old/pid:[1]", "/pid:[1]", False),
        ("old/", "new/pid:[1]", False),
        ("old/pid:[1]", "new/", False),
    ],
)
def test_previous_boot_requires_two_readable_boot_qualified_namespaces(recorded, current, expected):
    assert process.previous_boot(recorded, current) is expected


def test_a_record_this_host_launched_before_its_namespace_restarted_retires_without_signalling(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.swarm.commands.hive_id", lambda: "hive-a")
    local, calls, ended = adapter(tmp_path, monkeypatch, {PID: proc()})
    agent = record(namespace=WORKER)
    agent = replace(agent, runtime_target={**agent.runtime_target, "host_id": "hive-a"})
    router = RuntimeRouter([local])
    assert router.terminate(agent, ("/scratch/t1",)) == Outcome(
        "terminate", Status.OK, LOCAL, detail="its process ended with a previous boot"
    )
    assert (ended, calls) == ([], [])
    assert router.rejected == {}


def test_a_record_of_this_host_is_still_refused_when_the_local_namespace_is_unreadable(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.swarm.commands.hive_id", lambda: "hive-a")
    local, calls, ended = adapter(tmp_path, monkeypatch, {PID: proc()}, namespace="")
    agent = record(namespace=WORKER)
    agent = replace(agent, runtime_target={**agent.runtime_target, "host_id": "hive-a"})
    assert RuntimeRouter([local]).terminate(agent) == Outcome(
        "terminate", Status.REFUSED, LOCAL, Unqualified.NO_NAMESPACE, "local process namespace is unreadable"
    )
    assert (ended, calls) == ([], [])


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


def test_a_local_launch_admitted_by_the_local_runtime_is_retired_through_the_tick_runtime(tmp_path, monkeypatch):
    with bounded("the admitted launch retire"):
        store = launch_store()
        local, calls, ended = adapter(tmp_path, monkeypatch, {PID: proc()})
        started = local.admit(store, "sw", launched(store))
        tick_runtime = RoutedRuntime(local.herdr, RuntimeRouter([local]))
        assert tick_runtime.retire(store.execution("sw", started.execution_id), ("/scratch/t1",))
        assert ended == [(started.name, PID, ("/scratch/t1",), STARTED)]
        assert tick_runtime.refused == {}


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


@pytest.mark.parametrize(("owner", "closed"), [("engineer@a1b2c3-0009", False), (NAME, True), ("", True)])
def test_retire_process_closes_the_recorded_pane_only_while_it_belongs_to_the_ended_worker(
    tmp_path, monkeypatch, owner, closed
):
    pane = {"pane_id": "w1:p1", "name": owner, "agent_session": None}
    herdr, calls = herdr_runtime(tmp_path, monkeypatch, panes={"w1:p1": pane})
    assert herdr.retire_process(AgentRecord(NAME, "eng", "t1", pane_id="w1:p1"), PID, (), STARTED) is True
    assert (["pane", "close", "w1:p1"] in calls) is closed
    assert herdr.refusal(AgentRecord(NAME, "eng", "t1")) == {"process": 0, "refusal": "unknown"}


def test_retire_process_refuses_and_closes_nothing_when_herdr_cannot_name_the_pane_owner(tmp_path, monkeypatch):
    herdr, calls = herdr_runtime(tmp_path, monkeypatch)
    answer = herdr.herdr

    def unavailable(args):
        if args[:2] == ["agent", "get"]:
            calls.append(args)
            raise RuntimeError("herdr agent get: server unavailable")
        return answer(args)

    herdr.herdr = unavailable
    agent = AgentRecord(NAME, "eng", "t1", pane_id="w1:p1")
    assert herdr.retire_process(agent, PID, (), STARTED) is False
    assert calls == [["agent", "get", "w1:p1"]]
    assert herdr.refusal(agent) == {"process": PID, "refusal": "pane w1:p1: herdr agent get: server unavailable"}


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


def test_an_idle_remote_agent_is_retired_through_its_runtime_without_a_typed_nudge(remote_swarm):
    from scripts.gates import log
    from scripts.swarm.tick import IDLE_KILL_TICKS

    store, ledger, agent = remote_swarm
    store.update("sw", state="paused")
    runtime = TickRuntime()
    runtime.statuses[agent.name] = "idle"
    for n in range(IDLE_KILL_TICKS):
        tick("sw", store, ledger, runtime, now_ms=2_000 + n)
    assert runtime.nudged == []
    assert [row["at"] for row in log.recent("sw")][:2] == [2_000, 2_001]
    assert agent.name in runtime.homes and ledger.rows["t1"]["state"] == "open"


def test_each_acceptance_case_passes():
    from tests.sv2_run02_cases import case_a, case_b, case_c

    assert [case()["passed"] for case in (case_a, case_b, case_c)] == [True, True, True]
