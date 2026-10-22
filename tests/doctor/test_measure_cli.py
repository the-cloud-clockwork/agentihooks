import pytest

from scripts.doctor import cli as doctor
from scripts.doctor import loop
from scripts.swarm import cli as swarm_cli
from scripts.swarm.health.findings import Finding
from scripts.swarm.store import AgentRecord
from tests.doctor.test_doctor_cli import DOCTOR, STALE, WATCHED, env  # noqa: F401

NOW = 1_791_388_800_000
RETIRED = "master@d44a5a-0056"
LIVE = "engineer@d44a5a-0311"


def telemetry(agent, lag):
    return Finding(
        "telemetry stale",
        f"{agent}.{NOW - lag * 1000}",
        f"source events generated but unaccepted for {lag} seconds",
        (f"agent {agent}", "seat master@watch", "task unknown", "session s1"),
        "oldest unaccepted source event older than 60 seconds",
        lag,
    )


@pytest.fixture
def started(env, monkeypatch, capsys):  # noqa: F811
    store, _, _ = env
    assert doctor.main([WATCHED, "start"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(swarm_cli, "now_ms", lambda: NOW)
    return store


def detecting(monkeypatch, found=(), failing=False):
    def readers(store, ledger, slug, now, telemetry):
        assert (store is None, ledger is None, slug, now) == (False, False, WATCHED, NOW)
        assert telemetry is doctor.traces_read.measured_client
        read = {"trace": lambda: list(found)}
        return {**read, "inbox": lambda: 1 / 0} if failing else read

    monkeypatch.setattr(doctor.detect, "readers", readers)


def measured(capsys, *argv):
    code = doctor.main([WATCHED, "measure", *argv])
    output = capsys.readouterr()
    return code, output.out, output.err


def test_a_finding_the_doctor_never_saw_is_refused(started, monkeypatch, capsys):
    detecting(monkeypatch)
    code, out, err = measured(capsys, "telemetry-stale/nobody.1")
    assert (code, out) == (1, "")
    assert err == (
        "doctor: no finding telemetry-stale/nobody.1 has been seen; "
        "measure the class telemetry-stale to read every live target\n"
    )


def test_a_gone_finding_of_a_retired_agent_is_refused(started, monkeypatch, capsys):
    gone = telemetry(RETIRED, 600)
    loop.record(started, DOCTOR, [gone], NOW - 60_000, 0)
    detecting(monkeypatch)
    code, out, err = measured(capsys, gone.id)
    assert (code, out) == (1, "")
    assert err == (
        f"doctor: {gone.id} names agent {RETIRED}, which is no longer live; "
        "measure the class telemetry-stale to read every live target\n"
    )


def test_a_gone_finding_of_a_live_agent_reads_zero(started, monkeypatch, capsys):
    started.put_agent(WATCHED, AgentRecord(name=LIVE, lane="eng", task="t1"))
    gone = telemetry(LIVE, 600)
    loop.record(started, DOCTOR, [gone], NOW - 60_000, 0)
    detecting(monkeypatch)
    assert measured(capsys, gone.id) == (0, f"{gone.id} 0\n", "")


def test_a_gone_finding_of_a_seat_still_occupied_reads_zero(started, monkeypatch, capsys):
    started.put_agent(WATCHED, AgentRecord(name=LIVE, lane="master", task="", seat="master@watch"))
    gone = telemetry("master@watch", 600)
    loop.record(started, DOCTOR, [gone], NOW - 60_000, 0)
    detecting(monkeypatch)
    assert measured(capsys, gone.id) == (0, f"{gone.id} 0\n", "")


def test_an_absent_finding_is_unknown_while_a_detector_failed(started, monkeypatch, capsys):
    loop.record(started, DOCTOR, [STALE], NOW - 60_000, 0)
    detecting(monkeypatch, failing=True)
    code, out, err = measured(capsys, STALE.id)
    assert (code, out) == (1, "")
    assert err.splitlines() == [
        "the inbox detector failed: ZeroDivisionError: division by zero",
        f"doctor: {STALE.id} is unknown because the inbox detector failed: ZeroDivisionError: division by zero",
    ]


def test_a_reader_finding_is_refused_while_its_reader_timed_out(started, monkeypatch, capsys):
    def timed_out():
        raise TimeoutError

    monkeypatch.setattr(doctor.detect, "readers", lambda *args, **kwargs: {"ci": timed_out, "trace": timed_out})
    code, out, err = measured(capsys, f"trace-reader-unavailable/{WATCHED}.1")
    assert (code, out) == (1, "")
    assert err.splitlines() == [
        "the ci detector failed: TimeoutError: ",
        "the trace detector failed: TimeoutError: ",
        f"doctor: trace-reader-unavailable/{WATCHED}.1 is unknown because the ci detector failed: TimeoutError: ; "
        "the trace detector failed: TimeoutError: ",
    ]


def test_a_class_with_no_current_finding_is_refused_while_a_detector_failed(started, monkeypatch, capsys):
    loop.record(started, DOCTOR, [STALE], NOW - 60_000, 0)
    detecting(monkeypatch, failing=True)
    code, out, err = measured(capsys, "stale-claim")
    assert (code, out) == (1, "")
    assert err.splitlines() == [
        "the inbox detector failed: ZeroDivisionError: division by zero",
        "doctor: stale-claim is unknown because the inbox detector failed: ZeroDivisionError: division by zero",
    ]


def test_a_class_name_reads_every_live_finding_of_the_class(started, monkeypatch, capsys):
    loop.record(started, DOCTOR, [STALE], NOW - 60_000, 0)
    other = Finding("stale claim", "watch-eng-2", "claimed", ("task t2",), "30 minutes", 55)
    detecting(monkeypatch, (STALE, other, telemetry(LIVE, 70)), failing=True)
    code, out, err = measured(capsys, "stale-claim")
    assert (code, out) == (0, "stale-claim count 2 unknown 1 max 55\n")
    assert err == "the inbox detector failed: ZeroDivisionError: division by zero\n"


def test_a_class_name_the_doctor_never_saw_is_refused(started, monkeypatch, capsys):
    detecting(monkeypatch)
    code, out, err = measured(capsys, "stale-clam")
    assert (code, out) == (1, "")
    assert err == "doctor: no finding of the class stale-clam has been seen\n"


def test_the_telemetry_stale_class_reads_every_active_binding(started, monkeypatch, capsys):
    for name, session in ((LIVE, "s1"), ("engineer@d44a5a-0312", "s2"), ("engineer@d44a5a-0313", "")):
        started.put_agent(
            WATCHED, AgentRecord(name=name, lane="eng", task="t1", conversation_id=session, harness="codex")
        )
    started.put_agent(WATCHED, AgentRecord(name="engineer@gone", lane="eng", task="t9", state="finished"))
    progress = {
        ("s1", "codex"): {"generated_bytes": 10, "accepted_bytes": 4, "oldest_unaccepted": NOW - 9_000},
        ("s2", "codex"): {"generated_bytes": 4, "accepted_bytes": 4, "oldest_unaccepted": 0},
    }
    monkeypatch.setattr(doctor.registry, "progress", lambda session, harness: progress.get((session, harness)))
    detecting(monkeypatch)
    assert measured(capsys, "telemetry-stale") == (0, "telemetry-stale count 3 unknown 1 max 9\n", "")


def test_the_telemetry_stale_class_counts_a_live_langfuse_finding(started, monkeypatch, capsys):
    started.put_agent(WATCHED, AgentRecord(name=LIVE, lane="eng", task="t1", conversation_id="s1"))
    monkeypatch.setattr(doctor.registry, "progress", lambda session, harness: None)
    detecting(monkeypatch, (telemetry(LIVE, 75),))
    assert measured(capsys, "telemetry-stale") == (0, "telemetry-stale count 1 unknown 1 max 75\n", "")


def test_the_telemetry_stale_class_is_refused_when_the_trace_detector_fails(started, monkeypatch, capsys):
    monkeypatch.setattr(doctor.detect, "readers", lambda *args, **kwargs: {"trace": lambda: 1 / 0})
    code, out, err = measured(capsys, "exporter-backlog")
    assert (code, out) == (1, "")
    assert (
        err == "doctor: exporter-backlog is unknown: the trace detector failed: ZeroDivisionError: division by zero\n"
    )


def test_a_failed_spawn_id_for_a_task_the_ledger_lacks_is_refused(started, monkeypatch, capsys):
    monkeypatch.setattr(doctor.spawn_read, "records", lambda *a, **kw: {"slug": WATCHED, "actions": []})
    code, out, err = measured(capsys, "failed-spawn/mu9")
    assert (code, out) == (1, "")
    assert err == "doctor: failed-spawn/mu9 names no task on the watched ledger\n"
