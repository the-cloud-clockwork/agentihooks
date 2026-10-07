import copy

from scripts.doctor import health
from tests.doctor.recorded import load

MIN = 60_000


def recorded():
    return load("health")


def ids(found):
    return sorted(f.id for f in found)


def test_recorded_findings_left_without_a_verdict_are_raised():
    rec = recorded()
    assert ids(health.findings(rec["records"], rec["now_ms"], reported=set(rec["records"]))) == [
        "unjudged-finding/idle-with-claim/rig-grade-swarm-eng-32",
        "unjudged-finding/idle-with-claim/rig-grade-swarm-eng-34",
        "unjudged-finding/idle-with-claim/rig-grade-swarm-eng-45",
        "unjudged-finding/idle-with-claim/rig-grade-swarm-eng-52",
    ]


def test_a_finding_judged_within_the_limit_raises_nothing():
    rec = recorded()
    record = rec["records"]["idle-with-claim/rig-grade-swarm-eng-52"]
    assert health.unjudged({"x": record}, record["seen_at"] + 29 * MIN, 30) == []
    assert ids(health.unjudged({"x": record}, record["seen_at"] + 30 * MIN, 30)) == ["unjudged-finding/x"]


def test_planted_fault_a_judged_finding_that_came_back_is_raised_with_its_verdict():
    rec = recorded()
    records = copy.deepcopy(rec["records"])
    planted = records["over-monitoring/rig-grade-swarm-master-1"]
    assert health.returned(records) == []
    planted.update(returned=True, measure=planted["verdict"]["measure"] + 9)
    [found] = health.returned(records)
    assert found.id == "returned-finding/over-monitoring/rig-grade-swarm-master-1"
    assert found.measure == 9
    assert any("false-positive by rig-grade-swarm-master-1" in line for line in found.evidence)
    assert "measure 36 at the verdict, 45 now" in found.evidence


def test_only_findings_the_current_health_pass_reports_are_judged():
    rec = recorded()
    reported = {"idle-with-claim/rig-grade-swarm-eng-52", "over-monitoring/rig-grade-swarm-master-1"}
    assert ids(health.findings(rec["records"], rec["now_ms"], reported=reported)) == [
        "unjudged-finding/idle-with-claim/rig-grade-swarm-eng-52",
    ]
    records = copy.deepcopy(rec["records"])
    planted = records["over-monitoring/rig-grade-swarm-master-1"]
    planted.update(returned=True, measure=planted["verdict"]["measure"] + 9)
    assert ids(health.findings(records, rec["now_ms"], reported=set())) == []
    assert ids(health.findings(records, rec["now_ms"], reported=reported)) == [
        "returned-finding/over-monitoring/rig-grade-swarm-master-1",
        "unjudged-finding/idle-with-claim/rig-grade-swarm-eng-52",
    ]
