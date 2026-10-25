import pytest

from scripts.swarm_ledger import ledger_kinds


@pytest.mark.parametrize("kind", ["code", "ci"])
def test_post_merge_code_contract_requires_both_proof_fields(kind):
    task = {"kind": kind, "contract": {"after": "yes"}}
    assert ledger_kinds.after(task) is True
    assert ledger_kinds.unmet(task) == ["command", "output"]
    task["proof"] = {"command": "probe"}
    assert ledger_kinds.unmet(task) == ["output"]
    task["proof"] = {"output": "ready"}
    assert ledger_kinds.unmet(task) == ["command"]
    task["proof"] = {"command": "probe", "output": "ready"}
    assert ledger_kinds.unmet(task) == []


@pytest.mark.parametrize(
    "task", [{}, {"contract": {}}, {"contract": {"after": "no"}}, {"kind": "tune", "contract": {"after": "yes"}}]
)
def test_plain_and_other_task_kinds_keep_their_proof_policy(task):
    assert ledger_kinds.after(task) is False


@pytest.mark.parametrize("at", ["", "1000"])
def test_post_merge_contract_accepts_optional_maturity(at):
    ledger_kinds.check({"contract": {"after": "yes", "after_at": at}})


@pytest.mark.parametrize("at", ["-1", "1.5", "tomorrow", "１２３"])
def test_post_merge_contract_refuses_invalid_maturity(at):
    with pytest.raises(ValueError, match="^contract after_at must be Unix milliseconds or empty$"):
        ledger_kinds.check({"contract": {"after": "yes", "after_at": at}})


@pytest.mark.parametrize("after", ["true", "Yes", ""])
def test_post_merge_contract_flag_is_explicit(after):
    with pytest.raises(ValueError, match="^contract after must be yes or no$"):
        ledger_kinds.check({"contract": {"after": after}})
