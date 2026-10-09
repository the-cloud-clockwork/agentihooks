import copy
import json
import subprocess
from dataclasses import asdict

import pytest

from scripts.doctor import master_launches, spawn_read
from scripts.swarm.health.findings import Finding
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
    assert found.evidence[2].startswith("binding: no launched master bound since 2026-10-09 19:57")


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
    seen = [master_launches.findings(master_launches.as_of(record, t)) for t in times]
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
    [unavailable] = master_launches.findings({**record, "transfers": []})
    assert unavailable.id == "master-launch-evidence-unavailable/rig-grade-swarm"
    assert unavailable.evidence == ("journal: journalctl: not found",)
    again = master_launches.findings({**record, "transfers": [], "journal_error": "journalctl timed out"})
    assert (again[0].id, again[0].measure) == (unavailable.id, unavailable.measure)


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


def test_a_finding_back_after_its_verdict_stays_shown_until_the_next_verdict():
    from scripts.swarm.health.findings import Finding

    record = outage()
    times = failure_times(record)
    measures = iter((5, 1))

    def falling(record, at):
        return [Finding("failed spawn", "master", "s", (f"e{at}",), "t", next(measures))]

    verdicts = {master_launches.OLD_ID: {"verdict": {"at": 0, "measure": 2, "evidence": []}}}
    window = (times[0] - 1, times[1] + 1)
    assert master_launches.missed(record, replay(record, verdicts), window, (falling,)) == 0


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


def test_master_reader_reads_the_journal_from_the_last_live_binding():
    store, record = _store_with_outage()
    seen = []
    spawn_read.master_records(store, "rig-grade-swarm", run=_journal(record, seen))
    live = max(r["binding"]["at"] for r in record["transfers"] if r["binding"]["state"] == "live")
    assert seen[0][4:6] == ["--since", f"@{live / 1000:.3f}"]
    assert "--until" not in seen[0]


def test_master_reader_with_no_binding_reads_from_the_first_master_transfer():
    import fakeredis

    from scripts.swarm.store import RedisStore, SwarmConfig

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 1, 0))
    row = {
        "id": "f1",
        "reason": "fresh",
        "task": "master",
        "successor": "m@1",
        "at": 7000,
        "binding": {"state": "absent"},
    }
    store.redis.hset(store.key("sw", "transfers"), "f1", json.dumps(row))
    seen = []
    spawn_read.master_records(store, "sw", run=_journal({"journal": []}, seen))
    assert seen[0][4:6] == ["--since", "@7.000"]


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


def bare(transfers=(), restored=(), agents=(), journal=()):
    return {
        "slug": "sw",
        "transfers": list(transfers),
        "restored": list(restored),
        "agents": list(agents),
        "journal": list(journal),
        "journal_error": "",
    }


def absent(row_id="a1", at=1000, reason="recycle"):
    binding = {"state": "absent", "at": at + 5}
    return {"id": row_id, "reason": reason, "task": "master", "successor": f"m@{row_id}", "at": at, "binding": binding}


def spawn_line(at, error="boom"):
    return {"at": at, "pid": "9", "message": f"sw: master spawn failed: {error}"}


def test_the_recorded_outage_finding_is_exact():
    record = outage()
    last = failure_times(record)[-1]
    assert master_launches.findings(master_launches.as_of(record, last + master_launches.MATCH_MS)) == [
        Finding(
            "master launch failed",
            "rig-grade-swarm@1791575842404",
            "12 failed master launches with no live master after them",
            (
                f"error: {ERROR}",
                "attempt: transfer ac248650eba744098a0de8da11ada7cd for master@323133-0092, recycle handoff at "
                "2026-10-09 20:25:21 UTC",
                "binding: no launched master bound since 2026-10-09 19:57:22 UTC",
            ),
            "a failed master launch with no live binding after it",
            12,
        )
    ]


def test_the_unavailable_finding_is_exact():
    record = {**bare(), "journal": None, "journal_error": "gone"}
    assert master_launches.findings(record) == [
        Finding(
            "master launch evidence unavailable",
            "sw",
            "the tick journal is unreadable, so fresh master launch failures and launch errors are unavailable",
            ("journal: gone",),
            "the journal cannot be read",
            1,
        )
    ]


def test_sanitize_removes_a_strict_only_secret():
    token = "Bearer " + "a" * 30
    assert token not in master_launches.sanitize(f"refused {token}")


def test_times_read_in_utc():
    assert master_launches._iso(0) == "1970-01-01 00:00:00 UTC"
    assert master_launches._iso(61_500) == "1970-01-01 00:01:01 UTC"


@pytest.mark.parametrize(
    "line_at,error",
    [(1000, "boom"), (1000 + master_launches.MATCH_MS, "boom"), (1001 + master_launches.MATCH_MS, None)],
)
def test_a_journal_error_matches_within_one_minute_of_the_failure(line_at, error):
    found = master_launches.attempts(bare([absent()], journal=[spawn_line(line_at)]))
    assert found[0].error == (error or master_launches.NO_LINE)
    assert len(found) == (1 if error else 2)


def test_a_journal_line_before_the_failure_is_a_fresh_attempt():
    found = master_launches.attempts(bare([absent()], journal=[spawn_line(999)]))
    assert [(a.at, a.path, a.error) for a in found] == [
        (999, "fresh", "boom"),
        (
            1000,
            "recycle handoff",
            "unavailable: no master spawn failed line in the journal within a minute of the failure",
        ),
    ]


def test_an_unknown_transfer_reason_is_its_own_path():
    [found] = master_launches.attempts(bare([absent(reason="operator")], journal=[spawn_line(1000)]))
    assert found.path == "operator"


def test_a_restore_outcome_without_its_transfer_row_is_an_attempt_and_a_resumed_one_is_not():
    outcome = {
        "name": "master@x-1",
        "lane": "master",
        "outcome": "awaiting-decision",
        "reason": "resume failed to start: gone",
        "at": 5000,
        "transfer": "r9",
    }
    resumed = {**outcome, "outcome": "resumed", "transfer": "r8"}
    assert master_launches.attempts(bare(restored=[outcome, resumed])) == [
        master_launches.Attempt(
            5000, "native resume", "restore of master@x-1 on transfer r9", "resume failed to start: gone"
        )
    ]


def test_failures_at_the_same_moment_are_both_kept():
    found = master_launches.attempts(bare([absent("a1"), absent("a2")]))
    assert sorted(a.identity for a in found) == ["transfer a1 for m@a1", "transfer a2 for m@a2"]


@pytest.mark.parametrize(
    "agent,binding_at,failed",
    [
        ({"started_at": 1001, "state": "working"}, None, False),
        ({"started_at": 1001, "state": "starting"}, None, True),
        (None, 1000, False),
        (None, 999, True),
    ],
)
def test_a_live_binding_or_working_master_after_the_failure_resolves_it(agent, binding_at, failed):
    rows = [absent()]
    if binding_at is not None:
        rows.append({**absent("b1", at=1), "binding": {"state": "live", "at": binding_at}})
    record = bare(rows, agents=[agent] if agent else [], journal=[spawn_line(1000)])
    assert bool(master_launches.findings(record)) is failed


def test_a_failure_at_time_zero_with_no_binding_is_found():
    [found] = master_launches.findings(bare([absent(at=0)], journal=[spawn_line(0)]))
    assert found.measure == 1


def test_as_of_keeps_what_was_written_by_then_and_hides_later_bindings():
    live = {
        "id": "l1",
        "reason": "recycle",
        "task": "master",
        "successor": "m",
        "at": 10,
        "binding": {"state": "live", "at": 50},
    }
    unstamped = {**live, "id": "l2", "binding": {"state": "live"}}
    later = {**live, "id": "l3", "at": 51}
    record = bare(
        [live, unstamped, later],
        restored=[{"at": 50}, {"at": 51}],
        agents=[{"started_at": 50}, {"started_at": 51}],
        journal=[{"at": 50}, {"at": 51}],
    )
    assert master_launches.as_of(record, 50) == {
        **record,
        "transfers": [live, unstamped],
        "restored": [{"at": 50}],
        "agents": [{"started_at": 50}],
        "journal": [{"at": 50}],
    }
    assert master_launches.as_of(record, 49)["transfers"] == [{**live, "binding": {"state": "pending"}}, unstamped]


def test_the_old_detector_replay_reads_exactly_the_last_hour():
    at = 10 * master_launches.JOURNAL_MS
    lines = [
        spawn_line(at - master_launches.JOURNAL_MS - 1),
        spawn_line(at - master_launches.JOURNAL_MS),
        spawn_line(at),
        spawn_line(at + 1),
    ]
    assert [f.measure for f in master_launches.journal_hour(bare(journal=lines), at)] == [2]


@pytest.mark.parametrize(
    "measure,evidence,at,shown",
    [
        (1, ("a",), 5, False),
        (2, ("b",), 10**9, False),
        (3, ("b",), 5 + 100, True),
        (3, ("b",), 5 + 99, False),
    ],
)
def test_a_judged_finding_returns_only_when_it_grew_with_new_evidence_after_the_cooldown(measure, evidence, at, shown):
    verdict = {"at": 5, "measure": 2 if measure > 1 else 1, "evidence": ["a"]}
    finding = Finding("master launch failed", "sw@1", "s", evidence, "t", measure)
    assert master_launches._shown(finding, verdict, at, 100) is shown


def test_a_finding_judged_at_the_pass_time_and_returned_stays_shown():
    finding = Finding("master launch failed", "sw@1", "s", ("e1",), "t", 5)
    played = master_launches.Replay({finding.id: {"verdict": {"at": 100, "measure": 2, "evidence": []}}}, 0, 1)
    returned = {}
    assert master_launches._seen(finding, 100, played, returned) is True
    assert returned == {finding.id: 100}
    fell = Finding("master launch failed", "sw@1", "s", ("e2",), "t", 1)
    assert master_launches._seen(fell, 200, played, returned) is True


def test_the_missed_window_includes_its_edges():
    record = outage()
    times = failure_times(record)
    for edge in (times[0], times[-1]):
        assert master_launches.missed(record, replay(record), (edge, edge), (master_launches.journal_hour,)) == 1
