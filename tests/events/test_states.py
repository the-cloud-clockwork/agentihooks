import pytest

from scripts.events.states import EXECUTION, HIVE, MACHINES, SLOT, TASK, IllegalTransition, transition

pytestmark = pytest.mark.unit


def edges(table):
    return {(current, target) for current, targets in table.items() for target in targets}


def test_task_table_follows_the_ledger_task_states():
    assert edges(TASK) == {
        ("open", "claimed"),
        ("claimed", "open"),
        ("claimed", "blocked"),
        ("claimed", "pr"),
        ("claimed", "done"),
        ("blocked", "open"),
        ("blocked", "claimed"),
        ("pr", "claimed"),
        ("pr", "open"),
        ("pr", "done"),
    }
    assert TASK["done"] == ()


def test_execution_table_ends_once():
    assert edges(EXECUTION) == {("starting", "working"), ("starting", "ended"), ("working", "ended")}
    assert EXECUTION["ended"] == ()


def test_hive_table_joins_lives_and_leaves():
    assert edges(HIVE) == {
        ("joining", "live"),
        ("joining", "removed"),
        ("live", "stale"),
        ("live", "draining"),
        ("live", "removed"),
        ("stale", "live"),
        ("stale", "removed"),
        ("draining", "removed"),
    }
    assert HIVE["removed"] == ()


def test_slot_table_follows_the_account_slot_states():
    assert edges(SLOT) == {("reserved", "occupied"), ("reserved", "ended"), ("occupied", "ended")}
    assert SLOT["ended"] == ()


def test_machines_name_every_table_and_targets_are_known_states():
    assert MACHINES == {"task": TASK, "execution": EXECUTION, "hive": HIVE, "slot": SLOT}
    for table in MACHINES.values():
        assert all(target in table for targets in table.values() for target in targets)


@pytest.mark.parametrize("machine", sorted(MACHINES))
def test_transition_returns_the_target_for_every_allowed_edge(machine):
    for current, target in edges(MACHINES[machine]):
        assert transition(machine, current, target) == target


def refusal(machine, current, target):
    with pytest.raises(IllegalTransition) as raised:
        transition(machine, current, target)
    return raised.value


def test_transition_refuses_an_unknown_machine():
    error = refusal("seat", "open", "claimed")
    assert str(error) == "unknown state machine 'seat'"
    assert error.reason == "unknown state machine 'seat'"


def test_transition_refuses_an_unknown_current_state():
    assert str(refusal("task", "parked", "open")) == "task has no state 'parked'"


def test_transition_refuses_an_unknown_target_state():
    assert str(refusal("task", "open", "parked")) == "task has no state 'parked'"


def test_transition_refuses_leaving_a_terminal_state():
    assert str(refusal("execution", "ended", "working")) == "execution state 'ended' is terminal"


def test_transition_refuses_an_edge_the_table_lacks():
    assert str(refusal("task", "open", "done")) == "task cannot go from 'open' to 'done'"


def test_transition_refuses_staying_in_the_same_state():
    assert str(refusal("hive", "live", "live")) == "hive cannot go from 'live' to 'live'"


def test_illegal_transition_is_a_value_error():
    assert issubclass(IllegalTransition, ValueError)
