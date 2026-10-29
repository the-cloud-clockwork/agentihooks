import pytest

from scripts.doctor import registry, traces

NOW = 1_792_600_000_000


def _agent(**changes):
    return {
        "name": "engineer@hive-1",
        "state": "working",
        "started_at": 0,
        "launched_at": NOW - 60_000,
        **changes,
    }


def _findings(agent):
    [binding] = registry.bindings([agent])
    binding.update(read=True, traces=[], remote=None, local=None)
    return traces.active({"slug": "s", "now_ms": NOW, "active": [binding]}, traces.Limits())


@pytest.mark.parametrize("start", [0, None, -1])
def test_a_recent_hive_launch_keeps_launch_grace(start):
    assert _findings(_agent(started_at=start)) == []


def test_a_missing_start_uses_the_observed_hive_launch():
    agent = _agent()
    del agent["started_at"]
    assert _findings(agent) == []


@pytest.mark.parametrize("start", [0, None, -1])
def test_an_old_hive_launch_reports_its_observed_age(start):
    [found] = _findings(_agent(started_at=start, launched_at=NOW - 3_700_000))
    assert found.kind == "telemetry never exported"
    assert found.measure == 3700
    assert "after 3700 seconds" in found.summary


@pytest.mark.parametrize("start", [0, None, -1])
def test_an_unknown_start_reports_an_evidence_gap_without_an_age(start):
    [found] = _findings(_agent(started_at=start, launched_at=0))
    assert found.as_dict() == {
        "kind": "telemetry start unknown",
        "subject": "engineer@hive-1",
        "summary": "working agent start is unknown",
        "evidence": [
            "agent engineer@hive-1",
            "seat unknown",
            "task unknown",
            "session unknown",
            "no positive spawn or launch timestamp; launch grace cannot be established",
        ],
        "threshold": "an observed start is required to judge active telemetry",
    }
    assert found.measure == 1


def test_a_real_binding_start_keeps_its_lifetime_and_grace():
    assert _findings(_agent(started_at=NOW - 60_000, launched_at=NOW - 600_000)) == []


def test_a_launch_fallback_preserves_the_correlation_life():
    [binding] = registry.bindings([_agent()])
    assert binding["started_at"] == NOW - 60_000
    assert binding["life"] == "engineer@hive-1#0"


@pytest.mark.parametrize("start", [0, None, -1])
def test_a_raw_binding_with_an_unknown_start_cannot_bypass_grace(start):
    [binding] = registry.bindings([_agent(started_at=NOW - 600_000)])
    binding.update(started_at=start, read=True, traces=[], remote=None, local=None)
    [found] = traces.active({"slug": "s", "now_ms": NOW, "active": [binding]}, traces.Limits())
    assert found.kind == "telemetry start unknown"
    assert found.measure == 1


def test_a_raw_binding_with_a_missing_start_reports_an_evidence_gap():
    [binding] = registry.bindings([_agent()])
    del binding["started_at"]
    binding.update(read=True, traces=[], remote=None, local=None)
    [found] = traces.active({"slug": "s", "now_ms": NOW, "active": [binding]}, traces.Limits())
    assert found.kind == "telemetry start unknown"


def test_a_missing_spawn_and_launch_start_reports_an_evidence_gap():
    agent = _agent()
    del agent["started_at"], agent["launched_at"]
    [found] = _findings(agent)
    assert found.kind == "telemetry start unknown"
