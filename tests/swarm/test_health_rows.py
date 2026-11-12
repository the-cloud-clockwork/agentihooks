import pytest

from scripts.swarm.health import findings as health
from scripts.swarm.health import rows as finding_rows

NOW = 1_794_500_000_000
MIN = 60_000
WORKER = "sw-eng-1"


def ev(kind: str, target: str, by: str = WORKER, at: int = NOW) -> dict:
    return {"kind": kind, "target": target, "by": by, "at": at}


def tasks() -> dict[str, dict]:
    return {
        "t1": {"id": "t1", "title": "Build the panel", "state": "claimed", "claimed_by": WORKER, "phase": "p1"},
        "t0": {"id": "t0", "title": "Earlier work", "state": "done", "claimed_by": "sw-eng-9", "pr_url": "u"},
    }


def ceremony_events() -> list[dict]:
    chatter = [ev("comment edited", "phases/p9", at=NOW + n) for n in range(22)]
    required = [
        ev("task pr", "tasks/t1", at=NOW + 30),
        ev("comment added", "phases/p1", at=NOW + 31),
        ev("comment added", "tasks/t1", at=NOW + 32),
    ]
    excluded = [ev("joined", "", at=NOW + 33), ev("left", "", at=NOW + 34)]
    done = [ev("task done", "tasks/t0", at=NOW - 10 * MIN)]
    return done + chatter + required + excluded


@pytest.mark.feature("swarm-health-finding-rows")
def test_ceremony_stores_the_transitions_it_counted_and_the_last_outcome():
    events = ceremony_events()
    found = health.ceremony(events, tasks(), health.Limits())
    [finding] = finding_rows.attach(found, events, tasks(), [])
    assert finding.rows_total == 26
    assert len(finding.rows) == finding_rows.ROW_LIMIT
    assert [r["kind"] for r in finding.rows[-3:]] == ["task pr", "comment added", "comment added"]
    assert all(r["kind"] not in health.NOT_TRANSITIONS for r in finding.rows)
    assert finding.last_outcome.startswith("Earlier work at ")
    assert finding.as_dict()["rows_total"] == 26


@pytest.mark.feature("swarm-health-finding-rows")
def test_rows_doctrine_or_the_task_required_are_marked():
    events = ceremony_events()
    [finding] = finding_rows.attach(health.ceremony(events, tasks(), health.Limits()), events, tasks(), [])
    marks = {(r["kind"], r["target"]): r["required"] for r in finding.rows}
    assert marks[("task pr", "tasks/t1")] == "doctrine"
    assert marks[("comment added", "phases/p1")] == "task"
    assert marks[("comment added", "tasks/t1")] == "task"
    assert marks[("comment edited", "phases/p9")] == ""
    pr = next(r for r in finding.rows if r["kind"] == "task pr")
    assert pr["text"] == "Build the panel"
    assert pr["at"] == NOW + 30


@pytest.mark.feature("swarm-health-finding-rows")
def test_a_task_finding_lists_its_task_rows_and_its_claimant_outcome():
    events = [ev("task started", "tasks/t1", by="", at=NOW + n) for n in range(4)]
    events.append(ev("task done", "tasks/t0", at=NOW - MIN))
    found = health.proof_loops(events, tasks(), health.Limits(), {})
    [finding] = finding_rows.attach(found, events, tasks(), [])
    assert [r["kind"] for r in finding.rows] == ["task started"] * 4
    assert finding.rows[0]["required"] == ""
    assert finding.last_outcome.startswith("Earlier work at ")


@pytest.mark.feature("swarm-health-finding-rows")
def test_a_seat_finding_reads_the_rows_of_the_agent_in_the_seat():
    seat = health.Finding("master blocked on prompt", "master@rig", "s", ("e",), "t", 4)
    events = [ev("comment added", "chat", by="master@rig-1")]
    agents = [{"name": "master@rig-1", "seat": "master@rig"}]
    [finding] = finding_rows.attach([seat], events, tasks(), agents)
    assert [r["target"] for r in finding.rows] == ["chat"]
    assert finding.last_outcome == "none recorded"


@pytest.mark.feature("swarm-health-finding-rows")
def test_status_text_prints_every_finding_with_its_rows_and_marks():
    events = ceremony_events()
    [finding] = finding_rows.attach(health.ceremony(events, tasks(), health.Limits()), events, tasks(), [])
    text = finding_rows.text({"id": finding.id, **finding.as_dict(), "verdict": None})
    lines = text.splitlines()
    assert lines[0] == f"finding  ceremony  {WORKER}: more ledger transitions than outcomes"
    assert "  last outcome Earlier work at " in text
    assert f"  rows latest {finding_rows.ROW_LIMIT} of 26" in lines
    assert any(line.endswith("task pr  tasks/t1  Build the panel  required by doctrine") for line in lines)
    assert any(line.endswith("comment added  phases/p1  required by the task") for line in lines)


@pytest.mark.feature("swarm-health-finding-rows")
def test_status_text_says_when_a_finding_has_no_rows():
    bare = health.Finding("stalled", "nobody", "busy", ("pane working",), "10 minutes", 12)
    [finding] = finding_rows.attach([bare], [], {}, [])
    text = finding_rows.text({"id": finding.id, **finding.as_dict(), "verdict": None})
    assert "  rows none in the ledger events" in text.splitlines()


@pytest.mark.feature("swarm-health-finding-rows")
def test_the_verdict_record_stores_the_rows_and_the_last_outcome():
    import json

    from fakeredis import FakeRedis

    from scripts.swarm.health.verdicts import VerdictStore

    events = ceremony_events()
    [finding] = finding_rows.attach(health.ceremony(events, tasks(), health.Limits()), events, tasks(), [])
    board = VerdictStore(FakeRedis(decode_responses=True), "findings")
    [shown] = board.visible([finding], NOW, MIN)
    assert shown["rows"][-1]["target"] == "tasks/t1"
    record = json.loads(board.redis.hget("findings", finding.id))
    assert record["rows"] == shown["rows"]
    assert record["last_outcome"] == finding.last_outcome


@pytest.mark.feature("swarm-health-proof-loop-parks")
def test_a_task_claimed_four_times_with_three_parks_is_no_proof_loop():
    first = {"kind": "task started", "target": "tasks/t1", "at": NOW, "head": None}
    parked = [{**first, "at": NOW + n, "after": "park"} for n in (1, 2, 3)]
    tasks_ = {"t1": {"id": "t1", "state": "claimed"}}
    assert health.proof_loops([first, *parked], tasks_, health.Limits(), {}) == []
    plain = [{**first, "at": NOW + n} for n in (1, 2, 3)]
    [loop] = health.proof_loops([first, *plain], tasks_, health.Limits(), {})
    assert loop.evidence[1] == "started 4 times (3 reruns)"
    rounds = health.proof_loops([first, *parked], tasks_, health.Limits(), {"t1": {"h": 4}})
    assert rounds[0].evidence[2] == "4 review rounds"
