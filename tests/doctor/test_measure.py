import pytest

from scripts.doctor import measure
from scripts.swarm.health.findings import Finding
from scripts.swarm.store import SwarmError

NOW = 1_791_388_800_000


def found(kind, subject, value, agent=""):
    return Finding(kind, subject, "summary", (f"agent {agent}",) if agent else ("task t1",), "limit", value)


def test_a_current_finding_reads_its_measure_even_while_another_detector_failed():
    finding = found("turn gap", "s1", 42, "eng@a")
    assert measure.exact(finding.id, [finding], ["the ci detector failed"], {}, set()) == 42


def test_an_absent_finding_is_unknown_naming_every_failed_detector():
    failed = ["the ci detector failed: TimeoutError: ", "the trace detector failed: TimeoutError: "]
    with pytest.raises(SwarmError) as refused:
        measure.exact("turn-gap/s1", [], failed, {"turn-gap/s1": {"evidence": []}}, set())
    assert str(refused.value) == (
        "turn-gap/s1 is unknown because the ci detector failed: TimeoutError: ; "
        "the trace detector failed: TimeoutError: "
    )


def test_an_unseen_finding_is_refused_with_its_class():
    with pytest.raises(SwarmError) as refused:
        measure.exact("turn-gap/s1", [], [], {}, set())
    assert (
        str(refused.value)
        == "no finding turn-gap/s1 has been seen; measure the class turn-gap to read every live target"
    )


def test_a_gone_finding_naming_a_retired_agent_is_refused():
    seen = {"turn-gap/s1": {"evidence": ["task t1", "agent eng@a"]}}
    with pytest.raises(SwarmError) as refused:
        measure.exact("turn-gap/s1", [], [], seen, {"eng@b"})
    assert str(refused.value) == (
        "turn-gap/s1 names agent eng@a, which is no longer live; measure the class turn-gap to read every live target"
    )


@pytest.mark.parametrize("evidence,live", [(["agent eng@a"], {"eng@a"}), (["task t1"], set())])
def test_a_gone_finding_of_a_live_or_unnamed_agent_reads_zero(evidence, live):
    assert measure.exact("turn-gap/s1", [], [], {"turn-gap/s1": {"evidence": evidence}}, live) == 0


def test_a_class_reads_its_current_findings_and_counts_failed_detectors_as_unknown():
    current = [found("stale claim", "a", 40), found("stale claim", "b", 55), found("turn gap", "c", 90)]
    read = measure.of_class("stale-claim", current, ["x", "y"], set())
    assert read == measure.Read(2, 2, 55)
    assert read.line("stale-claim") == "stale-claim count 2 unknown 2 max 55"


def test_a_seen_class_with_no_current_finding_reads_zero():
    assert measure.of_class("stale-claim", [found("turn gap", "c", 9)], [], {"stale-claim"}) == measure.Read(0, 0, 0)


def test_a_class_with_no_current_finding_is_unknown_while_a_detector_failed():
    with pytest.raises(SwarmError, match="^stale-claim is unknown because the trace detector failed: x; y$"):
        measure.of_class(
            "stale-claim", [found("turn gap", "c", 9)], ["the trace detector failed: x", "y"], {"stale-claim"}
        )


def test_an_unseen_class_is_refused():
    with pytest.raises(SwarmError, match="^no finding of the class stale-clam has been seen$"):
        measure.of_class("stale-clam", [found("stale claim", "a", 40)], [], {"stale-claim"})


def test_a_lag_class_reads_every_binding_and_its_current_findings():
    current = [found("telemetry stale", "a.1", 75), found("exporter backlog", "b.1", 500)]
    assert measure.of_lag_class("telemetry-stale", current, [9, None, 0]) == measure.Read(3, 1, 75)
    assert measure.of_lag_class("exporter-backlog", [], [9, None]) == measure.Read(2, 1, 9)
    assert measure.of_lag_class("exporter-backlog", [], []) == measure.Read(0, 0, 0)


def test_lag_is_unknown_without_exporter_progress():
    assert measure.lag(None, NOW) is None
    local = {"generated_bytes": 10, "accepted_bytes": 4, "oldest_unaccepted": NOW - 9_000}
    assert measure.lag(local, NOW) == 9
    assert measure.lag({**local, "accepted_bytes": 10}, NOW) == 0


def test_a_failed_spawn_count_reads_a_known_task_or_the_master():
    failures = [found("failed spawn", "mu1", 2)]
    assert measure.spawn_count("failed-spawn/mu1", failures, {"mu1"}) == 2
    assert measure.spawn_count("failed-spawn/mu2", failures, {"mu2"}) == 0
    assert measure.spawn_count("failed-spawn/master", [], set()) == 0


def test_a_failed_spawn_count_refuses_a_task_the_ledger_lacks():
    with pytest.raises(SwarmError, match="^failed-spawn/mu9 names no task on the watched ledger$"):
        measure.spawn_count("failed-spawn/mu9", [], {"mu1"})
