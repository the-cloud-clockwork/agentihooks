import copy
import json
import subprocess
from dataclasses import asdict

import pytest

from scripts.doctor import master_launches, spawn_read
from tests.doctor.recorded import load

pytestmark = pytest.mark.xdist_group("fakeredis")

ERROR = "unsupported transfer: saved effort is outside the current swarm range"
COOLDOWN_MS = 60 * 60_000
INTERVAL_MS = 10 * 60_000


def outage():
    record = load("master_outage")
    return {**record, "journal_error": ""}


def failure_times(record):
    return [a.at for a in master_launches.attempts(record)]


def test_recorded_outage_is_one_finding_with_the_real_error_attempt_and_missing_binding():
    record = outage()
    times = failure_times(record)
    assert len(times) == 12
    [found] = master_launches.findings(master_launches.as_of(record, times[-1] + master_launches.MATCH_MS))
    assert found.id == f"master-launch-failed/rig-grade-swarm@{times[0]}"
    assert found.measure == 12
    assert found.evidence[0] == f"error: {ERROR}"
    assert found.evidence[1].startswith("attempt: transfer ac248650")
    assert "master@323133-0092" in found.evidence[1]
    assert "recycle handoff" in found.evidence[1]
    assert found.evidence[2].startswith("binding: no master bound since 2026-10-09 19:57")


def test_every_recorded_attempt_carries_its_journal_error():
    record = outage()
    assert {a.error for a in master_launches.attempts(record)} == {ERROR}
    assert {a.path for a in master_launches.attempts(record)} == {"recycle handoff"}


def test_recovered_outage_and_successful_launches_give_no_finding():
    record = outage()
    assert master_launches.findings(record) == []
    bound = [r for r in record["transfers"] if r["binding"]["state"] == "live"]
    assert master_launches.findings({**record, "transfers": bound, "journal": []}) == []


def test_unchanged_failures_and_age_alone_change_nothing():
    record = outage()
    last = failure_times(record)[-1]
    early = master_launches.findings(master_launches.as_of(record, last + master_launches.MATCH_MS))
    again = master_launches.findings(master_launches.as_of(copy.deepcopy(record), last + master_launches.MATCH_MS))
    live_at = next(r["binding"]["at"] for r in record["transfers"] if r["successor"] == "master@323133-0093")
    later = master_launches.findings(master_launches.as_of(record, live_at - 1))
    assert early == again == later


def test_new_failed_attempts_grow_the_one_active_finding():
    record = outage()
    times = failure_times(record)
    seen = [master_launches.findings(master_launches.as_of(record, t + master_launches.MATCH_MS)) for t in times]
    assert all(len(found) == 1 for found in seen)
    assert {found[0].id for found in seen} == {seen[0][0].id}
    assert [found[0].measure for found in seen] == list(range(1, 13))


def test_fresh_launch_failure_comes_from_the_journal_alone():
    record = {
        "slug": "sw",
        "transfers": [],
        "restored": [],
        "agents": [],
        "journal": [{"at": 1000, "pid": "42", "message": "sw: master spawn failed: no claude account has seats"}],
        "journal_error": "",
    }
    [attempt] = master_launches.attempts(record)
    assert (attempt.path, attempt.error) == ("fresh", "no claude account has seats")
    assert "tick 42" in attempt.identity
    [found] = master_launches.findings(record)
    assert found.id == "master-launch-failed/sw@1000"


def test_promoted_engineer_line_is_not_a_second_attempt():
    record = outage()
    lines = [line for line in record["journal"] if "promoted" in line["message"]]
    assert len(lines) == 1
    assert len(master_launches.attempts(record)) == 12


def test_native_resume_failure_takes_its_restore_reason():
    row = {
        "id": "r1",
        "reason": "restore",
        "task": "master",
        "successor": "master@x-1",
        "at": 5000,
        "binding": {"state": "absent", "at": 6000, "session": "master@x-1"},
    }
    outcome = {
        "name": "master@x-1",
        "lane": "master",
        "task": "master",
        "outcome": "awaiting-decision",
        "reason": "resume failed to start: conversation gone",
        "at": 5000,
        "transfer": "r1",
    }
    record = {"slug": "sw", "transfers": [row], "restored": [outcome], "agents": [], "journal": [], "journal_error": ""}
    [attempt] = master_launches.attempts(record)
    assert (attempt.path, attempt.error) == ("native resume", "resume failed to start: conversation gone")
    assert "transfer r1" in attempt.identity
    [found] = master_launches.findings(record)
    assert found.evidence[0] == "error: resume failed to start: conversation gone"


def test_unreadable_journal_is_reported_as_unavailable():
    record = {**outage(), "journal": None, "journal_error": "journalctl: not found"}
    last = failure_times(record)[-1]
    [found] = master_launches.findings(master_launches.as_of(record, last + master_launches.MATCH_MS))
    assert found.evidence[0] == "error: unavailable: journalctl: not found"
    assert "fresh launch failures unavailable: journalctl: not found" in found.evidence
    with pytest.raises(master_launches.Unavailable, match="journalctl: not found"):
        master_launches.findings({**record, "transfers": []})


def test_error_text_is_sanitized():
    token = "ghp_" + "a" * 36
    record = {
        "slug": "sw",
        "transfers": [],
        "restored": [],
        "agents": [],
        "journal": [{"at": 1, "pid": "1", "message": f"sw: master spawn failed: bad   token {token}\n" + "x" * 900}],
        "journal_error": "",
    }
    [attempt] = master_launches.attempts(record)
    assert token not in attempt.error
    assert "  " not in attempt.error and "\n" not in attempt.error
    assert len(attempt.error) <= master_launches.ERROR_CHARS


def replay(record, verdicts=None):
    return master_launches.Replay(
        record["verdicts"] if verdicts is None else verdicts, COOLDOWN_MS, INTERVAL_MS, tuple(record["passes"])
    )


def test_missed_replay_counts_the_recorded_outage_before_and_none_after():
    record = outage()
    window = (min(failure_times(record)) - 1, max(failure_times(record)) + 1)
    before = master_launches.missed(record, replay(record), window, (master_launches.journal_hour,))
    after = master_launches.missed(record, replay(record), window, master_launches.DETECTORS)
    assert (before, after) == (12, 0)


def test_replay_judges_each_failure_at_the_next_recorded_pass_or_one_interval_later():
    record = outage()
    times = failure_times(record)
    played = replay(record)
    assert played.pass_after(times[0]) == min(record["passes"][0], times[0] + INTERVAL_MS)
    assert played.pass_after(times[-1]) == record["passes"][2]
    assert played.pass_after(record["passes"][-1] + 1) == record["passes"][-1] + 1 + INTERVAL_MS


def test_missed_replay_counts_a_failure_the_doctor_never_passed_before_recovery():
    record = outage()
    times = failure_times(record)
    window = (times[0] - 1, times[-1] + 1)
    late = master_launches.Replay(record["verdicts"], COOLDOWN_MS, 10 * 60 * INTERVAL_MS, ())
    assert master_launches.missed(record, late, window, master_launches.DETECTORS) == 12


def test_missed_replay_ignores_verdicts_given_after_the_failure():
    record = outage()
    times = failure_times(record)
    finding_id = f"master-launch-failed/rig-grade-swarm@{times[0]}"
    late = {"verdict": {"at": times[-1] + 10**7, "measure": 99, "evidence": []}}
    verdicts = {**record["verdicts"], finding_id: late}
    window = (times[0] - 1, times[-1] + 1)
    assert master_launches.missed(record, replay(record, verdicts), window, master_launches.DETECTORS) == 0


def test_missed_replay_counts_only_failures_inside_the_window():
    record = outage()
    times = failure_times(record)
    window = (times[5] - 1, times[7] + 1)
    assert master_launches.missed(record, replay(record), window, (master_launches.journal_hour,)) == 3


def test_missed_replay_refuses_an_unreadable_journal():
    record = {**outage(), "journal": None, "journal_error": "journalctl timed out"}
    with pytest.raises(master_launches.Unavailable, match="journalctl timed out"):
        master_launches.missed(record, replay(record, {}), (0, 10**14), master_launches.DETECTORS)


def test_a_failed_row_without_a_retry_is_timed_from_its_attach_not_its_moving_binding_stamp():
    row = {
        "id": "a1",
        "reason": "recycle",
        "task": "master",
        "successor": "master@x-2",
        "at": 100,
        "attached_at": 200,
        "binding": {"state": "absent", "at": 900, "session": "master@x-2"},
    }
    record = {"slug": "sw", "transfers": [row], "restored": [], "agents": [], "journal": [], "journal_error": ""}
    restamped = {**record, "transfers": [{**row, "binding": {**row["binding"], "at": 1900}}]}
    assert [a.at for a in master_launches.attempts(record)] == [200]
    assert master_launches.findings(record) == master_launches.findings(restamped)


def _store_with_outage():
    import fakeredis

    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

    record = outage()
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("rig-grade-swarm", "/repo", 1, 0))
    for row in record["transfers"]:
        store.redis.hset(store.key("rig-grade-swarm", "transfers"), row["id"], json.dumps({**row, "handoff": ""}))
    other = {"id": "e1", "reason": "recycle", "task": "t1", "successor": "e@1", "at": 1, "binding": {"state": "absent"}}
    store.redis.hset(store.key("rig-grade-swarm", "transfers"), "e1", json.dumps(other))
    store.put_agent("rig-grade-swarm", AgentRecord("engineer@1", "eng", "t1", state="working"))
    return store, record


def _journal(record, seen):
    lines = "".join(
        json.dumps({"MESSAGE": line["message"], "_PID": line["pid"], "__REALTIME_TIMESTAMP": str(line["at"] * 1000)})
        + "\n"
        for line in record["journal"]
    )

    def run(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, lines, "")

    return run


def test_master_reader_is_read_only_and_reads_master_rows_and_journal():
    store, record = _store_with_outage()
    before = store.export("rig-grade-swarm")
    seen = []
    read = spawn_read.master_records(store, "rig-grade-swarm", run=_journal(record, seen), since="@1", until="@2")
    assert store.export("rig-grade-swarm") == before
    assert {r["id"] for r in read["transfers"]} == {r["id"] for r in record["transfers"]}
    assert read["agents"] == []
    assert read["journal"] == record["journal"]
    assert read["journal_error"] == ""
    argv = seen[0]
    assert argv[:4] == ["journalctl", "--user", "-u", "agentihooks-swarm.service"]
    assert ["--since", "@1", "--until", "@2"] == argv[4:8]
    assert "master spawn failed" in argv
    assert master_launches.findings(read) == []


def test_master_reader_treats_no_matching_lines_as_empty():
    store, _ = _store_with_outage()

    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "")

    read = spawn_read.master_records(store, "rig-grade-swarm", run=run)
    assert (read["journal"], read["journal_error"]) == ([], "")


@pytest.mark.parametrize(
    "failure,error",
    [
        (subprocess.CompletedProcess([], 1, "", "Failed to open journal"), "journalctl exit 1: Failed to open journal"),
        (FileNotFoundError("journalctl"), "FileNotFoundError: journalctl"),
        (subprocess.TimeoutExpired("journalctl", 30), "TimeoutExpired"),
    ],
)
def test_master_reader_reports_an_unreadable_journal(failure, error):
    store, _ = _store_with_outage()

    def run(argv, **kwargs):
        if isinstance(failure, Exception):
            raise failure
        return failure

    read = spawn_read.master_records(store, "rig-grade-swarm", run=run)
    assert read["journal"] is None
    assert read["journal_error"].startswith(error)


def test_master_reader_keeps_only_master_agents():
    store, record = _store_with_outage()
    from scripts.swarm.store import AgentRecord

    master = AgentRecord("master@x-1", "master", "master", state="working", started_at=7)
    store.put_agent("rig-grade-swarm", master)
    read = spawn_read.master_records(store, "rig-grade-swarm", run=_journal(record, []))
    assert read["agents"] == [asdict(master)]


def test_doctor_passes_reads_the_logged_pass_times():
    seen = []
    line = json.dumps({"MESSAGE": "sw-doctor: doctor pass: 3 new findings", "__REALTIME_TIMESTAMP": "5000000"})

    def run(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, line + "\n", "")

    assert spawn_read.doctor_passes("sw-doctor", "@1", "@2", run=run) == (5000,)
    assert "sw-doctor: doctor pass" in seen[0]


def test_doctor_passes_refuses_an_unreadable_journal():
    from scripts.swarm.store import SwarmError

    def run(argv, **kwargs):
        raise FileNotFoundError("journalctl")

    with pytest.raises(SwarmError, match="Doctor passes unavailable: FileNotFoundError: journalctl"):
        spawn_read.doctor_passes("sw-doctor", "@1", "@2", run=run)
