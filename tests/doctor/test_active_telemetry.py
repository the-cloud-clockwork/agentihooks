import pytest

from scripts.doctor import traces

LIMITS = traces.Limits()
NOW = 1_791_400_000_000
STARTED = NOW - 600_000
AGENT = "engineer@323133-0001"
LIFE = f"{AGENT}#{STARTED}"


def _binding(**changes):
    found = {
        "agent": AGENT,
        "life": LIFE,
        "seat": "eng-1@s",
        "task": "t1",
        "harness": "claude",
        "profile": "engineer",
        "started_at": STARTED,
        "session_id": "sid-1",
        "read": True,
        "traces": [
            {
                "id": "tr-1",
                "session_id": "sid-1",
                "life": LIFE,
                "seat": "eng-1@s",
                "task": "t1",
                "harness": "claude",
                "profile": "engineer",
            }
        ],
        "remote": {"trace": "tr-1", "fresh_ms": NOW - 5_000, "observations": 40},
        "local": {
            "generated_bytes": 1000,
            "accepted_bytes": 1000,
            "oldest_unaccepted": 0,
            "pending": 0,
            "overflow": 0,
            "accepted": 40,
            "accepted_at": NOW - 5_000,
            "exporter_alive": True,
            "requested_at": NOW - 3_000,
        },
    }
    found.update(changes)
    return found


def _record(*bindings, failures=(), down_since=0, historical=None):
    return {
        "slug": "s",
        "now_ms": NOW,
        "traces": [],
        "sessions": [],
        "merged": [],
        "active": list(bindings),
        "reader": {
            "failures": list(failures),
            "down_since": down_since,
            "active": {"bindings": len(bindings), "read": sum(1 for b in bindings if b["read"])},
            "historical": historical or {"traces": 0, "covered": 0, "complete": True},
        },
    }


def _local(**changes):
    return {**_binding()["local"], **changes}


def test_a_healthy_working_binding_raises_nothing():
    assert traces.findings(_record(_binding()), LIMITS) == []


def test_a_healthy_idle_binding_with_old_freshness_raises_nothing():
    idle = _binding(
        remote={"trace": "tr-1", "fresh_ms": NOW - 7_200_000, "observations": 40},
        local=_local(accepted_at=NOW - 7_200_000, requested_at=NOW - 7_200_000, exporter_alive=False),
    )
    assert traces.findings(_record(idle), LIMITS) == []


def test_a_binding_with_no_trace_past_its_grace_is_never_exported_and_names_seat_and_task():
    [found] = traces.findings(_record(_binding(traces=[], remote=None, local=None, session_id="")), LIMITS)
    assert found.kind == "telemetry never exported"
    assert found.id == f"telemetry-never-exported/{AGENT}.{STARTED}"
    assert "seat eng-1@s" in found.evidence and "task t1" in found.evidence
    assert f"no trace tagged swarm:s and agent:{AGENT}" in found.evidence
    assert found.measure == 600


def test_a_binding_inside_its_grace_is_not_judged():
    young = _binding(traces=[], remote=None, local=None, started_at=NOW - 30_000)
    assert traces.findings(_record(young), LIMITS) == []


def test_generated_progress_left_unaccepted_past_the_threshold_is_stale_even_with_a_live_exporter():
    stuck = _binding(local=_local(generated_bytes=5000, oldest_unaccepted=NOW - 90_000, exporter_alive=True))
    [found] = traces.findings(_record(stuck), LIMITS)
    assert found.kind == "telemetry stale"
    assert found.id == f"telemetry-stale/{AGENT}.{NOW - 90_000}"
    assert found.measure == 90
    assert "oldest unaccepted source event 90 seconds old" in found.evidence
    assert "generated 5000 bytes, accepted 1000 bytes" in found.evidence
    assert "exporter process alive" in found.evidence
    assert "last hook request 3 seconds ago" in found.evidence
    assert "Langfuse last accepted event 5 seconds ago" in found.evidence
    assert "session sid-1" in found.evidence
    assert "every 10 minutes" in found.threshold and "60 seconds" in found.threshold


def test_unaccepted_progress_inside_the_threshold_is_not_stale():
    fresh = _binding(local=_local(generated_bytes=5000, oldest_unaccepted=NOW - 30_000))
    assert traces.findings(_record(fresh), LIMITS) == []


def test_queued_observations_past_the_threshold_are_an_exporter_backlog():
    queued = _binding(
        local=_local(generated_bytes=5000, oldest_unaccepted=NOW - 120_000, pending=12, exporter_alive=False)
    )
    [found] = traces.findings(_record(queued), LIMITS)
    assert found.kind == "exporter backlog"
    assert "12 observations queued" in found.evidence
    assert "no exporter process" in found.evidence


def test_an_overflow_is_an_exporter_backlog():
    full = _binding(local=_local(generated_bytes=5000, oldest_unaccepted=NOW - 120_000, overflow=9_000_000))
    [found] = traces.findings(_record(full), LIMITS)
    assert found.kind == "exporter backlog"
    assert "pending cap overflow at 9000000 bytes" in found.evidence


def test_accepted_observations_missing_from_langfuse_are_a_stale_acknowledgement():
    lost = _binding(
        remote={"trace": "tr-1", "fresh_ms": NOW - 400_000, "observations": 30},
        local=_local(accepted_at=NOW - 120_000),
    )
    [found] = traces.findings(_record(lost), LIMITS)
    assert found.kind == "telemetry stale"
    assert found.id == f"telemetry-stale/{AGENT}.{NOW - 400_000}"
    assert "exporter recorded 40 accepted observations, Langfuse holds 30" in found.evidence
    assert found.measure == 120


def test_a_recent_acceptance_is_given_ingestion_time():
    ingesting = _binding(
        remote={"trace": "tr-1", "fresh_ms": NOW - 400_000, "observations": 30},
        local=_local(accepted_at=NOW - 20_000),
    )
    assert traces.findings(_record(ingesting), LIMITS) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [("life", f"{AGENT}#1"), ("seat", "eng-2@s"), ("task", "t2"), ("harness", "codex"), ("profile", "frontend")],
)
def test_a_trace_carrying_another_binding_is_misattributed(field, value):
    wrong = _binding()
    wrong["traces"][0][field] = value
    [found] = traces.findings(_record(wrong), LIMITS)
    assert found.kind == "telemetry misattributed"
    assert found.id == f"telemetry-misattributed/{AGENT}.{STARTED}.{field}={value}"
    expected = _binding()["traces"][0][field] if field != "profile" else "engineer"
    assert f"trace tr-1 {field} {value}, expected {expected}" in found.evidence


def test_a_new_misattribution_in_the_same_life_is_a_new_finding():
    profile = _binding()
    profile["traces"][0]["profile"] = "frontend"
    harness = _binding()
    harness["traces"][0]["harness"] = "codex"
    [first] = traces.findings(_record(profile), LIMITS)
    [again] = traces.findings(_record(harness), LIMITS)
    assert first.id.startswith(f"telemetry-misattributed/{AGENT}.{STARTED}")
    assert again.id.startswith(f"telemetry-misattributed/{AGENT}.{STARTED}")
    assert again.id != first.id


def test_a_lasting_misattribution_keeps_its_finding_while_the_session_grows():
    early = _binding(traces=[], remote=None)
    later = _binding(traces=[], remote=None, local=_local(accepted=90))
    [first] = traces.findings(_record(early), LIMITS)
    [again] = traces.findings(_record(later), LIMITS)
    assert again.id == first.id == f"telemetry-misattributed/{AGENT}.{STARTED}.unattributed"


def test_a_missing_remote_field_is_not_a_mismatch():
    partial = _binding()
    partial["traces"][0]["profile"] = ""
    assert traces.findings(_record(partial), LIMITS) == []


def test_a_session_accepted_under_another_agent_is_misattributed_not_never_exported():
    elsewhere = _binding(traces=[], remote=None)
    [found] = traces.findings(_record(elsewhere), LIMITS)
    assert found.kind == "telemetry misattributed"
    assert f"session sid-1 accepted 40 observations, none under agent:{AGENT}" in found.evidence


def test_an_unread_binding_is_not_judged_on_remote_evidence_and_the_outage_is_its_own_alarm():
    unread = _binding(read=False, traces=[], remote=None)
    found = traces.findings(_record(unread, failures=["active read failed: ConnectError"], down_since=NOW), LIMITS)
    assert [f.kind for f in found] == ["trace reader unavailable"]
    [outage] = found
    assert outage.id == f"trace-reader-unavailable/s.{NOW}"
    assert "active read failed: ConnectError" in outage.evidence
    assert "active bindings read 0 of 1" in outage.evidence
    assert outage.measure == 1


def test_an_unread_binding_still_raises_its_local_backlog():
    unread = _binding(read=False, local=_local(generated_bytes=5000, oldest_unaccepted=NOW - 90_000, pending=3))
    kinds = {f.kind for f in traces.findings(_record(unread, failures=["timeout"], down_since=NOW), LIMITS)}
    assert kinds == {"exporter backlog", "trace reader unavailable"}


def test_incomplete_historical_backfill_is_disclosed():
    record = _record(historical={"traces": 300, "covered": 120, "complete": False})
    [found] = traces.findings(record, LIMITS)
    assert found.kind == "trace coverage partial"
    assert found.measure == 180
    assert "observations read for 120 of 300 traces" in found.evidence


def test_thresholds_read_from_the_environment():
    limits = traces.Limits.from_env(
        {"AGENTIHOOKS_DOCTOR_ACTIVE_STALE_SECONDS": "30", "AGENTIHOOKS_DOCTOR_INTERVAL_MINUTES": "2"}
    )
    assert (limits.active_stale_seconds, limits.active_grace_seconds, limits.interval_minutes) == (30, 60, 2)


def test_a_known_session_missing_under_its_agent_is_never_exported_even_beside_other_traces():
    other = _binding(session_id="sid-2", remote=None, local=None)
    [found] = traces.findings(_record(other), LIMITS)
    assert found.kind == "telemetry never exported"
    assert "session sid-2" in found.evidence


def test_an_incomplete_trace_listing_raises_no_untraced_session():
    record = _record(historical={"listed": False, "traces": 100, "covered": 100, "complete": False})
    record["sessions"] = [{"agent": AGENT, "session_id": "", "task": "t1", "started_at": 0}]
    [found] = traces.findings(record, LIMITS)
    assert found.kind == "trace coverage partial"
    assert found.measure == 1
    assert "trace listing stopped at its page cap" in found.evidence


def test_measures_report_each_active_binding_and_the_coverage():
    stuck = _binding(local=_local(generated_bytes=5000, oldest_unaccepted=NOW - 90_000))
    found = traces.measures(_record(stuck, historical={"traces": 3, "covered": 2, "complete": False}))
    assert found["active"] == [
        {
            "agent": AGENT,
            "seat": "eng-1@s",
            "task": "t1",
            "session": "sid-1",
            "read": True,
            "unaccepted_seconds": 90,
            "langfuse_fresh_seconds": 5,
        }
    ]
    assert found["coverage"]["historical"] == {"traces": 3, "covered": 2, "complete": False}
    assert found["coverage"]["active"] == {"bindings": 1, "read": 1}


CADENCE = "judged at each Doctor scan, every 10 minutes"
UNKNOWN = (f"agent {AGENT}", "seat unknown", "task unknown", "session unknown")


def test_a_never_exported_finding_reads_in_full():
    blank = _binding(seat="", task="", session_id="", traces=[], remote=None, local=None)
    assert traces.findings(_record(blank), LIMITS) == [
        traces.Finding(
            "telemetry never exported",
            f"{AGENT}.{STARTED}",
            "working agent with no Langfuse trace after 600 seconds",
            (*UNKNOWN, f"no trace tagged swarm:s and agent:{AGENT}"),
            f"no trace after a 60 second grace, {CADENCE}",
            600,
        )
    ]


def test_a_stale_finding_with_nothing_requested_and_no_langfuse_event_reads_in_full():
    quiet = _binding(
        remote=None,
        local=_local(generated_bytes=5000, oldest_unaccepted=NOW - 61_000, requested_at=0, exporter_alive=False),
    )
    [found] = [f for f in traces.findings(_record(quiet), LIMITS) if f.kind == "telemetry stale"]
    assert found.summary == "source events generated but unaccepted for 61 seconds"
    assert found.evidence[4:] == (
        "oldest unaccepted source event 61 seconds old",
        "generated 5000 bytes, accepted 1000 bytes",
        "0 observations queued",
        "no exporter process",
        "no hook request recorded",
        "Langfuse shows no accepted event",
    )
    assert found.threshold == f"oldest unaccepted source event older than 60 seconds, {CADENCE}"


def test_a_backlog_summary_says_queued_and_an_unread_binding_says_so():
    queued = _binding(read=False, local=_local(generated_bytes=5000, oldest_unaccepted=NOW - 90_000, pending=2))
    [found] = [f for f in traces.findings(_record(queued), LIMITS) if f.kind == "exporter backlog"]
    assert found.summary == "source events queued but unaccepted for 90 seconds"
    assert found.evidence[-1] == "Langfuse not read this scan"


def test_a_remote_with_no_event_time_shows_no_accepted_event():
    empty = _binding(
        remote={"trace": "tr-1", "fresh_ms": 0, "observations": 40},
        local=_local(generated_bytes=5000, oldest_unaccepted=NOW - 90_000),
    )
    [found] = traces.findings(_record(empty), LIMITS)
    assert found.evidence[-1] == "Langfuse shows no accepted event"
    assert traces.measures(_record(empty))["active"][0]["langfuse_fresh_seconds"] is None
    assert traces.measures(_record(_binding(remote=None)))["active"][0]["langfuse_fresh_seconds"] is None
    assert traces.measures(_record(_binding()))["active"][0]["unaccepted_seconds"] == 0


def test_a_stale_acknowledgement_reads_in_full():
    lost = _binding(
        remote={"trace": "tr-1", "fresh_ms": NOW - 400_000, "observations": 30},
        local=_local(accepted_at=NOW - 61_000),
    )
    [found] = traces.findings(_record(lost), LIMITS)
    assert found.summary == "accepted observations missing from Langfuse 61 seconds after acceptance"
    assert found.threshold == f"accepted observations absent from Langfuse past 60 seconds, {CADENCE}"
    assert found.evidence[4:] == (
        "exporter recorded 40 accepted observations, Langfuse holds 30",
        "last acceptance 61 seconds ago",
        "Langfuse last accepted event 400 seconds ago",
    )


def test_a_misattributed_finding_reads_in_full():
    wrong = _binding()
    wrong["traces"][0]["task"] = "t2"
    wrong["traces"][0]["seat"] = "eng-2@s"
    assert traces.findings(_record(wrong), LIMITS) == [
        traces.Finding(
            "telemetry misattributed",
            f"{AGENT}.{STARTED}.seat=eng-2@s+task=t2",
            f"telemetry of {AGENT} carries another binding",
            (
                f"agent {AGENT}",
                "seat eng-1@s",
                "task t1",
                "session sid-1",
                "trace tr-1 seat eng-2@s, expected eng-1@s",
                "trace tr-1 task t2, expected t1",
            ),
            "every trace tagged with the agent carries its life, seat, task, harness and resolved profile",
            2,
        )
    ]


@pytest.mark.parametrize(
    "binding",
    [
        _binding(traces=[], remote=None, local=None, started_at=NOW - 60_000),
        _binding(local=_local(generated_bytes=5000, oldest_unaccepted=NOW - 60_000)),
        _binding(
            remote={"trace": "tr-1", "fresh_ms": NOW - 400_000, "observations": 30},
            local=_local(accepted_at=NOW - 60_000),
        ),
    ],
    ids=["grace", "stale", "acknowledgement"],
)
def test_exactly_at_a_threshold_nothing_is_raised(binding):
    assert traces.findings(_record(binding), LIMITS) == []


def test_a_reader_outage_reads_in_full_and_keeps_five_failures():
    failures = [f"failure {n}" for n in range(7)]
    record = _record(_binding(read=False, traces=[], remote=None), failures=failures, down_since=NOW, historical=None)
    assert traces.reader(record, LIMITS) == [
        traces.Finding(
            "trace reader unavailable",
            f"s.{NOW}",
            "7 Langfuse reads failed or ran out of time",
            (
                *failures[:5],
                "active bindings read 0 of 1",
                "observations read for 0 of 0 traces",
                "unread bindings are not judged on Langfuse evidence",
            ),
            f"any failed or unfinished Langfuse read, {CADENCE}",
            7,
        )
    ]


def test_partial_coverage_reads_in_full_and_keeps_three_unreadable_traces():
    unreadable = [f"trace t{n} unreadable: TimeoutError" for n in range(5)]
    record = _record(historical={"traces": 10, "covered": 4, "complete": False, "unreadable": unreadable})
    assert traces.reader(record, LIMITS) == [
        traces.Finding(
            "trace coverage partial",
            "s",
            "observations of 6 traces not read yet",
            (
                "observations read for 4 of 10 traces",
                *unreadable[:3],
                "tool error, task cost and turn gap findings cover only the read traces",
                "each Doctor scan reads more within its read budget",
            ),
            "every trace's observations read",
            6,
        )
    ]
