from scripts.swarm.status import task_counts


def test_out_of_scope_tasks_count_in_no_state():
    tasks = [
        {"id": "a", "state": "open"},
        {"id": "b", "state": "open", "out_of_scope": True},
        {"id": "c", "state": "blocked", "out_of_scope": True},
        {"id": "d", "state": "blocked"},
        {"id": "e", "state": "done", "out_of_scope": True},
    ]

    assert task_counts(tasks) == {"open": 1, "claimed": 0, "blocked": 1, "pr": 0, "done": 0}


def test_status_counts_match_the_ledger_snapshot_states():
    from collections import Counter

    from scripts.swarm import metrics_ledger

    doc = {
        "tasks": [
            {"id": "a", "state": "open", "phase": "p1"},
            {"id": "b", "state": "open", "phase": "p1", "out_of_scope": True},
            {"id": "c", "state": "claimed", "phase": "p1"},
            {"id": "d", "state": "blocked", "phase": "p1", "out_of_scope": True},
        ],
    }
    snapshot = Counter(n["state"] for n in metrics_ledger.snapshot_nodes(doc) if n["kind"] == "task")

    assert task_counts(doc["tasks"]) == {
        state: snapshot[state] for state in ("open", "claimed", "blocked", "pr", "done")
    }
