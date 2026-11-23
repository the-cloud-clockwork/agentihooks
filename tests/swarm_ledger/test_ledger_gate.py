import ledger_gate
import ledger_hook
import pytest


def test_a_relay_counts_as_ledger_progress():
    assert "relay" in ledger_gate.WRITE_COMMANDS
    assert ledger_hook.touched('agentihooks ledger --slug s --as m relay questions/q "Use it." --quote "use it"')
    assert not ledger_hook.touched("agentihooks ledger --slug s --as m status")


@pytest.mark.feature("ledger-hook-bounded-read")
def test_task_targets_name_only_unhandled_operator_task_targets_once_each() -> None:
    events = [
        {"rev": 2, "by": "operator", "kind": "comment", "target": "tasks/t0"},
        {"rev": 3, "by": "operator", "kind": "comment", "target": "tasks/t1"},
        {"rev": 4, "by": "operator", "kind": "comment", "target": "chat"},
        {"rev": 5, "by": "engineer@a1-2", "kind": "comment", "target": "tasks/t2"},
        {"rev": 6, "by": "operator", "kind": "chat cleared", "target": "tasks/t3"},
        {"rev": 7, "by": "operator", "kind": "edit", "target": "tasks/t1"},
        {"rev": 8, "by": "operator", "kind": "edit", "target": "phases/p1"},
        {"by": "operator", "kind": "edit", "target": "tasks/t4"},
        {"rev": 9, "by": "operator", "kind": "edit"},
        {"rev": 10, "by": "operator", "kind": "edit", "target": "tasks/t5"},
    ]
    meta = {"members": {"engineer@a1-1": {"handled_rev": 2}}, "events": events}
    assert ledger_gate.task_targets(meta, "engineer@a1-1") == ("tasks/t1", "tasks/t5")
    assert ledger_gate.task_targets(meta, "stranger@a1-9") == ()
    fresh = {"members": {"engineer@a1-1": {}}, "events": [events[7], {**events[7], "rev": 1, "target": "tasks/t9"}]}
    assert ledger_gate.task_targets(fresh, "engineer@a1-1") == ("tasks/t9",)
