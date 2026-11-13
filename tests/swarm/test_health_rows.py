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
def test_ceremony_stores_the_transitions_it_counted_and_the_last_outcome() -> None:
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
def test_rows_doctrine_or_the_task_required_are_marked() -> None:
    events = [
        *ceremony_events(),
        ev("comment added", "tasks/t0", at=NOW + 35),
        ev("comment added", "tasks/t2", at=NOW + 36),
    ]
    held = {**tasks(), "t2": {"id": "t2", "title": "Finished", "state": "done", "claimed_by": WORKER, "phase": "p2"}}
    [finding] = finding_rows.attach(health.ceremony(events, held, health.Limits()), events, held, [])
    marks = {(r["kind"], r["target"]): r["required"] for r in finding.rows}
    assert marks[("task pr", "tasks/t1")] == "doctrine"
    assert marks[("comment added", "phases/p1")] == "task"
    assert marks[("comment added", "tasks/t1")] == "task"
    assert marks[("comment edited", "phases/p9")] == ""
    assert marks[("comment added", "tasks/t0")] == ""
    assert marks[("comment added", "tasks/t2")] == ""
    pr = next(r for r in finding.rows if r["kind"] == "task pr")
    assert pr["text"] == "Build the panel"
    assert pr["at"] == NOW + 30


@pytest.mark.feature("swarm-health-finding-rows")
def test_a_task_finding_lists_its_task_rows_and_its_claimant_outcome() -> None:
    events = [ev("task started", "tasks/t1", by="", at=NOW + n) for n in range(4)]
    events.append({**ev("task started", "tasks/t1", by="", at=NOW + 5), "after": "park"})
    events.append({**ev("comment added", "tasks/t1", by="sw-eng-7", at=NOW + 6), "text": "looked\n  at it"})
    events.append(ev("task done", "tasks/t0", at=NOW - MIN))
    found = health.proof_loops(events, tasks(), health.Limits(), {})
    [finding] = finding_rows.attach(found, events, tasks(), [])
    assert [r["kind"] for r in finding.rows] == ["task started"] * 5 + ["comment added"]
    assert [r["text"] for r in finding.rows[3:]] == ["Build the panel", "after park", "looked at it"]
    assert finding.rows[0]["required"] == ""
    assert finding.rows[-1]["required"] == ""
    assert finding.last_outcome.startswith("Earlier work at ")


@pytest.mark.feature("swarm-health-finding-rows")
def test_a_seat_finding_reads_the_rows_of_the_agent_in_the_seat() -> None:
    seat = health.Finding("master blocked on prompt", "master@rig", "s", ("e",), "t", 4)
    events = [ev("comment added", "chat", by="master@rig-1")]
    agents = [{"name": "master@rig-1", "seat": "master@rig"}]
    [finding] = finding_rows.attach([seat], events, tasks(), agents)
    assert [r["target"] for r in finding.rows] == ["chat"]
    assert finding.last_outcome == "none recorded"


@pytest.mark.feature("swarm-health-finding-rows")
def test_status_text_prints_every_finding_with_its_rows_and_marks() -> None:
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
def test_status_text_says_when_a_finding_has_no_rows() -> None:
    bare = health.Finding("stalled", "nobody", "busy", ("pane working",), "10 minutes", 12)
    [finding] = finding_rows.attach([bare], [], {}, [])
    text = finding_rows.text({"id": finding.id, **finding.as_dict(), "verdict": None})
    assert "  rows none in the ledger events" in text.splitlines()
    assert "  id stalled/nobody" in text.splitlines()


@pytest.mark.feature("swarm-health-finding-rows")
def test_a_row_without_a_time_reads_time_zero() -> None:
    bare = health.Finding("stale claim", "t1", "quiet", ("no change",), "30 minutes", 1)
    [finding] = finding_rows.attach([bare], [{"kind": "task pr", "target": "tasks/t1", "by": WORKER}], tasks(), [])
    assert [row["at"] for row in finding.rows] == [0]


@pytest.mark.feature("swarm-health-finding-rows")
def test_a_task_target_keeps_every_part_after_the_tasks_prefix() -> None:
    assert health.target_task("tasks/t1") == "t1"
    assert health.target_task("tasks/a/b") == "a/b"
    assert health.target_task("phases/p1") == ""
    assert health.target_task(None) == ""


@pytest.mark.feature("swarm-health-finding-rows")
def test_seat_rows_keep_the_agent_events_sorted_by_time_with_their_labels() -> None:
    seat = health.Finding("failed launch", WORKER, "launch failed", ("refused",), "1 launch", 1)
    events = [
        {"by": WORKER, "at": NOW + 3, "target": "tasks/t9"},
        {"kind": "launch failed", "by": WORKER, "at": NOW + 1, "error": "pane refused"},
        {"kind": "task pr", "by": WORKER, "at": NOW + 2, "target": "tasks/t3"},
        {"kind": "comment added", "by": "sw-eng-7", "at": NOW, "target": "chat"},
    ]
    held = {**tasks(), "t3": {"id": "t3", "state": "claimed", "claimed_by": "sw-eng-7"}}
    [finding] = finding_rows.attach([seat], events, held, [])
    assert [(r["kind"], r["target"], r["text"]) for r in finding.rows] == [
        ("launch failed", "", "pane refused"),
        ("task pr", "tasks/t3", ""),
        ("", "tasks/t9", ""),
    ]


@pytest.mark.feature("swarm-health-finding-rows")
def test_the_last_outcome_is_the_latest_done_task_with_an_outcome() -> None:
    seat = health.Finding("ceremony", WORKER, "s", ("e",), "t", 1)
    done = {"state": "done", "pr_url": "u"}
    held = {"tA": {"id": "tA", "title": "Alpha", **done}, "tB": {"id": "tB", "title": "Beta", **done}}
    events = [
        {"kind": "task done", "target": "tasks/tA", "by": WORKER},
        {"kind": "task done", "target": "tasks/tB", "by": WORKER, "at": NOW + MIN},
        {"kind": "task done", "target": "tasks/t9", "by": WORKER, "at": NOW + 3 * MIN},
        {"kind": "comment added", "target": "tasks/tA", "by": "sw-eng-7", "at": NOW + 5 * MIN},
    ]
    [finding] = finding_rows.attach([seat], events, held, [])
    assert finding.last_outcome == "Beta at 2026-11-12 16:14"


@pytest.mark.feature("swarm-health-finding-rows")
def test_status_text_prints_the_whole_block_in_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    monkeypatch.setenv("TZ", "Asia/Tokyo")
    time.tzset()
    rows = [
        {"at": NOW + MIN, "kind": "task pr", "target": "tasks/t1", "text": "Build", "required": "doctrine"},
        {"at": NOW + 2 * MIN, "kind": "comment edited", "target": "phases/p9", "text": "", "required": ""},
    ]
    record = {
        "id": f"ceremony/{WORKER}",
        "kind": "ceremony",
        "subject": WORKER,
        "summary": "s",
        "evidence": ["e"],
        "threshold": "t",
        "verdict": {"value": "false-positive"},
        "last_outcome": "o",
        "rows_total": 2,
        "rows": rows,
    }
    try:
        text = finding_rows.text(record)
    finally:
        monkeypatch.undo()
        time.tzset()
    assert text == (
        f"finding  ceremony  {WORKER}: s\n"
        "  - e\n"
        "  threshold t\n"
        f"  id ceremony/{WORKER}  earlier verdict false-positive\n"
        "  last outcome o\n"
        "  rows 2\n"
        "    2026-11-12 16:14  task pr  tasks/t1  Build  required by doctrine\n"
        "    2026-11-12 16:15  comment edited  phases/p9\n"
    )


@pytest.mark.feature("swarm-health-finding-rows")
def test_an_added_event_without_a_target_queues_nothing() -> None:
    added = [{"kind": "added", "by": WORKER, "at": NOW + n} for n in range(5)]
    assert health.scope_inflation(added, tasks(), health.Limits()) == []


@pytest.mark.feature("swarm-health-finding-rows")
def test_the_verdict_record_stores_the_rows_and_the_last_outcome() -> None:
    import json

    from fakeredis import FakeRedis

    from scripts.swarm.health.verdicts import VerdictStore

    events = ceremony_events()
    [finding] = finding_rows.attach(health.ceremony(events, tasks(), health.Limits()), events, tasks(), [])
    redis = FakeRedis(decode_responses=True)
    board = VerdictStore(redis, "findings")
    [shown] = board.visible([finding], NOW, MIN)
    assert shown["rows"][-1]["target"] == "tasks/t1"
    record = json.loads(str(redis.hget("findings", finding.id)))
    assert record["rows"] == shown["rows"]
    assert record["last_outcome"] == finding.last_outcome
    assert record["rows_total"] == 26


@pytest.mark.feature("swarm-health-proof-loop-parks")
def test_a_task_claimed_four_times_with_three_parks_is_no_proof_loop() -> None:
    first = {"kind": "task started", "target": "tasks/t1", "at": NOW, "head": None}
    parked = [{**first, "at": NOW + n, "after": "park"} for n in (1, 2, 3)]
    tasks_ = {"t1": {"id": "t1", "state": "claimed"}}
    assert health.proof_loops([first, *parked], tasks_, health.Limits(), {}) == []
    reopened = [{**first, "at": NOW + n, "after": "master"} for n in (1, 2, 3)]
    assert health.proof_loops([first, *reopened], tasks_, health.Limits(), {}) == []
    plain = [{**first, "at": NOW + n} for n in (1, 2, 3)]
    [loop] = health.proof_loops([first, *plain], tasks_, health.Limits(), {})
    assert loop.evidence[1] == "started 4 times (3 reruns)"
    rounds = health.proof_loops([first, *parked], tasks_, health.Limits(), {"t1": {"h": 4}})
    assert rounds[0].evidence[2] == "4 review rounds"
