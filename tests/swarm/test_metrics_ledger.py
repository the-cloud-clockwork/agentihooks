from copy import deepcopy

import pytest

from scripts.swarm import metrics_outbox

pytestmark = pytest.mark.unit

NOW = 1_800_000_000_000
PATH = {"plan": "plans/a", "phase": "phases/p", "slice": "slices/s", "task": "tasks/t"}


class Ledger:
    def __init__(self):
        self.doc = {
            "plans": [{"id": "a"}],
            "phases": [{"id": "p", "plan": "plans/a"}],
            "slices": [{"id": "s", "phase": "phases/p"}],
            "tasks": [{"id": "t", "phase": "p", "slice": "slices/s", "state": "open", "lane": "eng"}],
            "priorities": [],
            "time_left_minutes": 12,
            "_meta": {"rev": 0, "events": []},
        }
        self.nodes = [
            {"node": "plans/a", "kind": "plan", "parent": None, "depth": 0, "state": "open"},
            {"node": "phases/p", "kind": "phase", "parent": "plans/a", "depth": 1, "state": "building"},
            {"node": "slices/s", "kind": "slice", "parent": "phases/p", "depth": 2, "state": "open"},
            {"node": "tasks/t", "kind": "task", "parent": "slices/s", "depth": 3, "state": "open"},
        ]
        self.calls = []

    def state(self, slug):
        self.calls.append(("state", slug))
        return deepcopy(self.doc)

    def hierarchy(self, slug):
        self.calls.append(("hierarchy", slug))
        return deepcopy(self.nodes)

    def move(self, state, revision, at):
        self.doc["tasks"][0]["state"] = state
        self.nodes[-1]["state"] = state
        self.doc["_meta"]["rev"] = revision
        self.doc["_meta"]["events"].append(
            {"rev": revision, "at": at, "by": "worker", "kind": f"task {state}", "target": "tasks/t"}
        )


def read(box, table):
    return sorted(box.recent(table, NOW + 1000), key=lambda row: row["ts_ms"])


def test_four_task_states_become_distinct_rows_with_the_hierarchy_path(tmp_path):
    from scripts.swarm import metrics_ledger

    ledger = Ledger()
    path = tmp_path / "outbox.sqlite"
    box = metrics_outbox.Outbox(path, metrics_outbox.Settings("http://sink", "", ""), send=lambda *args: False)
    try:
        for revision, state in enumerate(("open", "claimed", "pr", "done"), 1):
            ledger.move(state, revision, NOW + revision)
            metrics_ledger.record(box, "example", NOW + revision, ledger)
        metrics_ledger.record(box, "example", NOW + 5, ledger)
        rows = read(box, "ledger_events")
        assert len(rows) == 4
        assert [row["state"] for row in rows] == ["open", "claimed", "pr", "done"]
        assert [row["ts_ms"] for row in rows] == [NOW + n for n in range(1, 5)]
        assert [row["revision"] for row in rows] == [1, 2, 3, 4]
        assert all({key: row[key] for key in PATH} == PATH for row in rows)
        assert all(row["ledger"] == "example" and row["target"] == "tasks/t" and row["lane"] == "eng" for row in rows)
        assert len({row["event_id"] for row in rows}) == 4
    finally:
        box.close()
    box = metrics_outbox.Outbox(path, metrics_outbox.Settings("http://sink", "", ""))
    try:
        metrics_ledger.record(box, "example", NOW + 6, ledger)
        assert read(box, "ledger_events") == rows
    finally:
        box.close()


def test_snapshot_after_a_phase_tick_shows_counts_priorities_time_left_and_freezes(tmp_path):
    from scripts.swarm import metrics_ledger

    ledger = Ledger()
    path = tmp_path / "outbox.sqlite"
    box = metrics_outbox.Outbox(path, metrics_outbox.Settings("http://sink", "", ""))
    try:
        metrics_ledger.record(box, "example", NOW, ledger)
        first = read(box, "ledger_snapshots")
        assert first
        ledger.move("done", 1, NOW + 299_999)
        ledger.doc["phases"][0]["done"] = True
        ledger.nodes[1]["state"] = "done"
        ledger.doc["time_left_minutes"] = 0
        ledger.doc["priorities"] = [{"id": "urgent", "item": "tasks/t", "at": NOW + 240_000}]
        ledger.doc["freezes"] = [
            {"verb": "freeze", "target": "plans/a", "author": "operator", "time": NOW, "reason": "focus elsewhere"},
            {"verb": "focus", "target": "lane:eng", "author": "operator", "time": NOW, "reason": "finish this lane"},
        ]
        metrics_ledger.record(box, "example", NOW + 299_999, ledger)
        assert read(box, "ledger_snapshots") == first
        metrics_ledger.record(box, "example", NOW + 300_000, ledger)
        rows = [row for row in read(box, "ledger_snapshots") if row["ts_ms"] == NOW + 300_000]
        counts = [row for row in rows if row["measure"] == "tasks" and row["lane"] == "eng"]
        for key, node in (("plan", "plans/a"), ("phase", "phases/p"), ("slice", "slices/s")):
            group = [row for row in counts if row["item"] == node]
            assert next(row["value"] for row in group if row["state"] == "done") == 1.0
            assert next(row["value"] for row in group if row["state"] == "open") == 0.0
            assert all(row[key] == node for row in group)
        phase = next(row for row in rows if row["measure"] == "nodes" and row["item"] == "phases/p")
        assert phase["state"] == "done" and phase["plan"] == "plans/a" and phase["value"] == 1.0
        priority = next(row for row in rows if row["measure"] == "priority")
        assert priority["item"] == "tasks/t" and priority["value"] == 60.0
        assert {key: priority[key] for key in PATH} == PATH
        assert next(row["value"] for row in rows if row["measure"] == "time_left") == 0.0
        freezes = [row for row in rows if row["measure"] == "freeze"]
        assert {(row["item"], row["state"]) for row in freezes} == {("plans/a", "freeze"), ("lane:eng", "focus")}
        assert all(row["value"] == 1.0 for row in freezes)
        assert len({row["event_id"] for row in rows}) == len(rows)
        assert all(row["ledger"] == "example" for row in rows)
    finally:
        box.close()
    box = metrics_outbox.Outbox(path, metrics_outbox.Settings("http://sink", "", ""))
    try:
        metrics_ledger.record(box, "example", NOW + 300_001, ledger)
        assert [row for row in read(box, "ledger_snapshots") if row["ts_ms"] == NOW + 300_000] == rows
        metrics_ledger.record(box, "example", NOW + 600_000, ledger)
        assert any(row["ts_ms"] == NOW + 600_000 for row in read(box, "ledger_snapshots"))
    finally:
        box.close()
