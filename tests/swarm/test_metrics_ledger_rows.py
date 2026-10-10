import hashlib
import json

import pytest

from scripts.swarm import metrics_ledger, metrics_outbox

pytestmark = pytest.mark.unit

NOW = 1_800_000_000_000
EMPTY = {"plan": "", "phase": "", "slice": "", "task": ""}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def snapshot_id(now_ms, measure, item, state, lane):
    return f"snapshot:sw:{now_ms}:{digest({'item': item, 'lane': lane, 'measure': measure, 'state': state})}"


def test_paths_default_task_lanes_to_eng_and_other_nodes_to_no_lane():
    nodes = [
        {"node": "phases/p", "kind": "phase", "parent": None},
        {"node": "tasks/a", "kind": "task", "parent": "phases/p"},
        {"node": "tasks/b", "kind": "task", "parent": "phases/p"},
    ]
    tasks = [{"id": "a"}, {"id": "b", "lane": "ci"}]
    assert metrics_ledger.paths(nodes, tasks) == {
        "phases/p": {**EMPTY, "phase": "phases/p", "lane": ""},
        "tasks/a": {**EMPTY, "phase": "phases/p", "task": "tasks/a", "lane": "eng"},
        "tasks/b": {**EMPTY, "phase": "phases/p", "task": "tasks/b", "lane": "ci"},
    }


def test_an_event_row_carries_every_column_with_a_sorted_payload():
    event = {"target": "questions/q", "rev": 3, "kind": "answered", "by": "operator", "at": NOW}
    payload = '{"at": 1800000000000, "by": "operator", "kind": "answered", "rev": 3, "target": "questions/q"}'
    assert (
        metrics_ledger.event_identity("sw", event, 2) == f"ledger:sw:2:{hashlib.sha256(payload.encode()).hexdigest()}"
    )
    assert metrics_ledger.event_row("sw", event, {}, True, "id") == {
        "event_id": "id",
        "ledger": "sw",
        "ts_ms": NOW,
        **EMPTY,
        "revision": 3,
        "kind": "answered",
        "by": "operator",
        "target": "questions/q",
        "lane": "",
        "state": "",
        "catch_up": 1,
        "first_missed": 0,
        "last_missed": 0,
        "payload": payload,
    }


def test_a_gap_row_names_the_missing_range_and_later_events_skip_the_cursor():
    events = [
        {"rev": 5, "at": NOW - 9, "by": "w", "kind": "task pr", "target": "tasks/x"},
        {"rev": 5, "at": NOW - 9, "by": "w", "kind": "task pr", "target": "tasks/x"},
        {"rev": 6, "at": NOW - 8, "by": "w", "kind": "task done", "target": "tasks/x"},
    ]
    rows = metrics_ledger.event_rows("sw", events, {}, 3, NOW)
    gap = rows[0]
    assert gap["event_id"] == "gap:sw:4:4"
    assert (gap["first_missed"], gap["last_missed"], gap["revision"], gap["ts_ms"]) == (4, 4, 4, NOW)
    assert (gap["by"], gap["kind"], gap["target"], gap["catch_up"]) == ("metrics", "history gap", "", 0)
    assert [row["event_id"] for row in rows[1:]] == [
        metrics_ledger.event_identity("sw", events[0], 0),
        metrics_ledger.event_identity("sw", events[1], 1),
        metrics_ledger.event_identity("sw", events[2], 0),
    ]
    assert [row["state"] for row in rows[1:]] == ["pr", "pr", "done"]
    assert [row["lane"] for row in rows] == ["", "", "", ""]


def test_no_gap_when_history_continues_the_cursor_and_the_cursor_revision_is_skipped():
    events = [
        {"rev": 4, "at": NOW, "by": "w", "kind": "task open", "target": "tasks/x"},
        {"rev": 5, "at": NOW, "by": "w", "kind": "task claimed", "target": "tasks/x"},
    ]
    rows = metrics_ledger.event_rows("sw", events, {}, 4, NOW)
    assert [row["revision"] for row in rows] == [5]
    assert [row["revision"] for row in metrics_ledger.event_rows("sw", events, {}, 3, NOW)] == [4, 5]
    assert metrics_ledger.event_rows("sw", events[1:], {}, 3, NOW)[0]["event_id"] == "gap:sw:4:4"


def test_a_snapshot_row_identity_hashes_its_sorted_fields():
    row = metrics_ledger.snapshot_row("sw", NOW, "time_left", "", {}, 3)
    assert row == {
        "event_id": snapshot_id(NOW, "time_left", "", "", ""),
        "ledger": "sw",
        "ts_ms": NOW,
        **EMPTY,
        "measure": "time_left",
        "item": "",
        "state": "",
        "lane": "",
        "value": 3.0,
    }


def test_task_counts_cover_every_known_lane_and_add_up_tasks():
    nodes = [
        {"node": "phases/p", "kind": "phase", "state": "building"},
        {"node": "tasks/a", "kind": "task", "state": "open"},
        {"node": "tasks/b", "kind": "task", "state": "open"},
    ]
    known = {
        "phases/p": {**EMPTY, "phase": "phases/p", "lane": ""},
        "tasks/a": {**EMPTY, "phase": "phases/p", "task": "tasks/a", "lane": "ci"},
        "tasks/b": {**EMPTY, "phase": "phases/p", "task": "tasks/b", "lane": "ci"},
    }
    rows = metrics_ledger.count_rows("sw", NOW, nodes, known)
    assert {row["item"] for row in rows} == {"", "phases/p"}
    assert {row["lane"] for row in rows} == {"", "ci", "eng", "plan"}
    open_ci = [row["value"] for row in rows if row["lane"] == "ci" and row["state"] == "open"]
    open_all = [row["value"] for row in rows if row["lane"] == "" and row["state"] == "open"]
    assert open_ci == [2.0, 2.0] and open_all == [2.0, 2.0]
    assert len(rows) == 2 * 4 * len(metrics_ledger.STATES)


def test_age_rows_skip_closed_items_and_age_open_ones_from_their_birth():
    known = {"tasks/t": {**EMPTY, "task": "tasks/t", "lane": "eng"}}
    doc = {
        "priorities": [{"id": "x", "item": "tasks/t", "at": NOW}, {"id": "y", "item": "tasks/u", "at": NOW - 4000}],
        "questions": [
            {"id": "done", "done": True},
            {"id": "scoped", "out_of_scope": True},
            {"id": "answered", "answers": [{"text": "yes"}]},
            {"id": "deleted", "answers": [{"text": "no", "deleted": True}]},
            {"id": "born"},
        ],
        "followups": [{"id": "unknown"}, {"id": "late", "at": NOW + 5000}],
    }
    births = {"questions/born": NOW - 2000}
    rows = metrics_ledger.age_rows("sw", NOW, doc, known, births)
    assert [(row["measure"], row["item"], row["value"]) for row in rows] == [
        ("priority", "tasks/t", 0.0),
        ("priority", "tasks/u", 4.0),
        ("question", "questions/deleted", -1.0),
        ("question", "questions/born", 2.0),
        ("followup", "followups/unknown", -1.0),
        ("followup", "followups/late", 0.0),
    ]
    assert rows[0]["task"] == "tasks/t" and rows[0]["lane"] == "eng"
    assert rows[1]["task"] == "" and rows[1]["lane"] == ""


def test_snapshot_rows_use_births_time_left_and_freeze_selectors():
    known = {"plans/a": {**EMPTY, "plan": "plans/a", "lane": ""}}
    doc = {
        "questions": [{"id": "q"}],
        "freezes": [
            {"verb": "freeze", "target": "plans/a"},
            {"verb": "focus", "target": "lane:ci"},
            {"verb": "freeze", "target": "kind:task"},
        ],
    }
    rows = metrics_ledger.snapshot_rows("sw", NOW, doc, [], known, {"questions/q": NOW - 1000})
    question = next(row for row in rows if row["measure"] == "question")
    assert question["value"] == 1.0
    left = next(row for row in rows if row["measure"] == "time_left")
    assert (left["item"], left["value"]) == ("", -1.0)
    freezes = [(row["item"], row["state"], row["lane"], row["plan"]) for row in rows if row["measure"] == "freeze"]
    assert freezes == [
        ("plans/a", "freeze", "", "plans/a"),
        ("lane:ci", "focus", "ci", ""),
        ("kind:task", "freeze", "", ""),
    ]


def test_snapshot_nodes_take_each_kind_state_from_a_document_missing_collections():
    doc = {
        "phases": [{"id": "p"}],
        "tasks": [{"id": "t", "phase": "p", "state": "claimed"}, {"id": "u", "phase": "p", "out_of_scope": True}],
        "_meta": {"events": []},
    }
    nodes = {row["node"]: row for row in metrics_ledger.snapshot_nodes(doc)}
    assert nodes["tasks/t"] == {"node": "tasks/t", "kind": "task", "parent": "phases/p", "state": "claimed"}
    assert nodes["tasks/u"]["state"] == "out_of_scope"
    assert nodes["phases/p"] == {"node": "phases/p", "kind": "phase", "parent": None, "state": "building"}


class Ledger:
    def __init__(self, doc):
        self.doc = doc

    def state(self, slug):
        assert slug == "sw"
        return json.loads(json.dumps(self.doc))


def recent(box, table):
    return box.recent(table, NOW + 10_000_000)


def test_record_keeps_first_births_and_cached_paths_across_passes(tmp_path):
    doc = {
        "phases": [{"id": "p"}],
        "tasks": [{"id": "t", "phase": "p", "state": "open"}],
        "questions": [{"id": "q"}],
        "_meta": {
            "rev": 2,
            "events": [
                {"rev": 1, "at": NOW - 9000, "by": "w", "kind": "commented", "target": "questions/q"},
                {"rev": 2, "at": NOW - 3000, "by": "w", "kind": "added", "target": "questions/q"},
            ],
        },
    }
    box = metrics_outbox.Outbox(tmp_path / "outbox.sqlite", metrics_outbox.Settings("http://sink", "", ""))
    try:
        metrics_ledger.record(box, "sw", NOW, Ledger(doc))
        question = next(row for row in recent(box, "ledger_snapshots") if row["measure"] == "question")
        assert question["value"] == 3.0
        del doc["tasks"]
        doc["_meta"] = {
            "rev": 3,
            "events": [{"rev": 3, "at": NOW + 1, "by": "w", "kind": "task done", "target": "tasks/t"}],
        }
        metrics_ledger.record(box, "sw", NOW + metrics_ledger.SNAPSHOT_MS, Ledger(doc))
        done = next(row for row in recent(box, "ledger_events") if row["revision"] == 3)
        assert (done["phase"], done["task"], done["lane"]) == ("phases/p", "tasks/t", "eng")
        later = [row for row in recent(box, "ledger_snapshots") if row["ts_ms"] == NOW + metrics_ledger.SNAPSHOT_MS]
        assert next(row["value"] for row in later if row["measure"] == "question") == 3.0 + 300.0
    finally:
        box.close()
