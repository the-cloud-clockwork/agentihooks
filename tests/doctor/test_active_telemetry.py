import copy

import pytest

from scripts.doctor import loop, traces
from scripts.swarm.store import RedisStore, SwarmConfig

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
    assert found.id == f"telemetry-misattributed/{AGENT}.{STARTED}"
    expected = _binding()["traces"][0][field] if field != "profile" else "engineer"
    assert f"trace tr-1 {field} {value}, expected {expected}" in found.evidence


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


@pytest.fixture
def store():
    import fakeredis

    found = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    found.create(SwarmConfig("watch-doctor", "/repo", 1, 0, state="drained", template="doctor"))
    return found


@pytest.mark.xdist_group("fakeredis")
def test_the_watcher_sends_one_finding_per_fault_clears_on_recovery_and_rearms_on_a_new_fault(store):
    def scan(binding, at):
        record = copy.deepcopy(_record(binding))
        record["now_ms"] = at
        found = traces.findings(record, LIMITS)
        return found, loop.record(store, "watch-doctor", found, at, 60 * 60_000)

    fault = _binding(local=_local(generated_bytes=5000, oldest_unaccepted=NOW - 90_000, pending=4))
    found, new = scan(fault, NOW)
    assert [n["kind"] for n in new] == ["exporter backlog"]
    later = copy.deepcopy(fault)
    later["local"]["generated_bytes"] = 9000
    found, new = scan(later, NOW + 600_000)
    assert len(found) == 1 and new == []
    found, new = scan(_binding(), NOW + 1_200_000)
    assert found == [] and new == []
    again = _binding(local=_local(generated_bytes=9000, oldest_unaccepted=NOW + 1_500_000, pending=2))
    found, new = scan(again, NOW + 1_800_000)
    assert [n["kind"] for n in new] == ["exporter backlog"]
    assert new[0]["id"] != f"exporter-backlog/{AGENT}.{NOW - 90_000}"
