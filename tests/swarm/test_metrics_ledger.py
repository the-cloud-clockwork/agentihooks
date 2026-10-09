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
