import copy

from scripts.doctor import handoffs
from scripts.swarm.health.findings import Finding
from tests.doctor.recorded import load


def recorded():
    return load("handoffs")


def ids(found):
    return sorted(f.id for f in found)


def test_recorded_handoffs_raise_the_ones_left_without_a_recap():
    rec = recorded()
    assert ids(handoffs.findings(rec["handoffs"])) == sorted(f"missing-recap/{name}" for name in rec["no_recap"])


def test_planted_fault_a_handoff_without_its_recap_is_raised():
    rec = recorded()
    records = copy.deepcopy(rec["handoffs"])
    planted = next(h for h in records if h["from"] not in rec["no_recap"])
    assert planted["from"] not in {f.subject for f in handoffs.missing_recap(records)}
    planted["recaps"] = [r for r in planted["recaps"] if r["occupant"] != planted["from"]]
    found = {f.subject: f for f in handoffs.missing_recap(records)}[planted["from"]]
    assert f"successor {planted['to']}" in found.evidence


def test_planted_fault_a_successor_asking_what_the_handoff_answered_is_raised():
    rec = recorded()
    records = copy.deepcopy(rec["handoffs"])
    planted = next(h for h in records if h["to"])
    assert handoffs.reasked(records) == []
    planted["asked"] = [
        {"id": "q1", "address": "master@x", "at": 1, "text": "Which branch and worktree hold the docs-uv-run change?"},
        {"id": "q2", "address": "master@x", "at": 2, "text": "Should I rebase onto the newest dev before review?"},
    ]
    [found] = handoffs.reasked(records)
    assert found.id == f"reasked-handoff/{planted['to']}"
    assert found.measure == 1
    assert any(line.startswith("message q1 to master@x") for line in found.evidence)


def _bare(**fields):
    return {
        "seat": "",
        "task": "doc-79030d0c5613b0f2",
        "from": "",
        "to": "",
        "at": 0,
        "document": "",
        "recaps": [],
        "asked": [],
        **fields,
    }


def test_a_handoff_without_seat_or_sender_is_reported_apart_naming_its_task():
    assert handoffs.missing_recap([_bare()]) == [
        Finding(
            "unattributed handoff",
            "doc-79030d0c5613b0f2",
            "handoff record names no seat and no sender",
            ("task doc-79030d0c5613b0f2 with no seat on record", "no sender on record", "successor not started yet"),
            "every handoff names its seat or its sender",
            1,
        )
    ]
    [found] = handoffs.missing_recap([_bare(to="engineer@x-2")])
    assert found.id == "unattributed-handoff/doc-79030d0c5613b0f2"
    assert found.evidence[2] == "successor engineer@x-2"


def test_a_handoff_with_only_its_seat_or_only_its_sender_stays_a_missing_recap():
    found = handoffs.missing_recap([_bare(), _bare(seat="eng-1@x"), _bare(**{"from": "engineer@x-1"})])
    assert [f.id for f in found] == [
        "unattributed-handoff/doc-79030d0c5613b0f2",
        "missing-recap/eng-1@x",
        "missing-recap/engineer@x-1",
    ]
