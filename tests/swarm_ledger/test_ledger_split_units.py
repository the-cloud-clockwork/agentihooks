import pytest

from scripts.swarm_ledger import ledger_agent_ops, ledger_tasks


@pytest.mark.parametrize("by", ["operator", "1bad", 7])
def test_an_agent_op_needs_an_agent_author(by):
    with pytest.raises(ValueError, match=r"^agent ops need `by`, an agent name other than operator$"):
        ledger_agent_ops.check({"op": "ack", "rev": 1, "by": by})


def test_a_gate_bypass_needs_an_integer_unhandled():
    with pytest.raises(ValueError, match=r"^gate_bypass needs an integer unhandled$"):
        ledger_agent_ops.check({"op": "gate_bypass", "by": "engineer@a-1", "unhandled": "2"})


def test_a_done_task_names_every_missing_proof_field():
    after = {"id": "t1", "kind": "ops", "state": "done"}
    refusal = ledger_tasks._done_refusal({"tasks": []}, {"item": "tasks/t1"}, after)
    assert refusal == "tasks/t1 cannot be done without its proof: command, output"


def test_an_update_parked_on_an_unknown_task_is_dropped():
    doc = {"tasks": [{"id": "t1"}]}
    op = {"item": "tasks/t1", "fields": {"parked_on": ["t9"]}}
    assert ledger_tasks._gated(doc, op, None, doc["tasks"][0]) is False
