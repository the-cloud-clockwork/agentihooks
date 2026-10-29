import pytest

from scripts.swarm_ledger import ledger_tasks
from tests.swarm_ledger.test_task_split import ENGINEER, ledger_dir, row, update  # noqa: F401

pytestmark = pytest.mark.unit

URL = "https://github.com/o/r/pull/9"


def test_an_engineer_records_the_scope_a_pull_request_delivers():
    scopes = {URL: {"kind": "prerequisite", "scope": "tests that accept both gate states"}}
    op = {"op": "task_update", "id": "u0", "by": ENGINEER, "item": "tasks/t1", "fields": {"pull_scopes": scopes}}
    assert ledger_tasks.check(op) is None
    state, rejected = update("t1", {"pull_scopes": scopes}, by=ENGINEER)
    assert rejected == []
    assert row(state, "t1")["pull_scopes"] == scopes


@pytest.mark.parametrize(
    "scopes",
    [
        [],
        {"pull/9": {"kind": "stage", "scope": "the gate"}},
        {"https://example.com/x": {"kind": "stage", "scope": "the gate"}},
        {f"{URL}/files": {"kind": "stage", "scope": "the gate"}},
        {f"{URL}\n": {"kind": "stage", "scope": "the gate"}},
        {URL: "the gate"},
        {URL: {"kind": "half", "scope": "the gate"}},
        {URL: {"kind": "stage", "scope": " "}},
        {URL: {"kind": "stage", "scope": 3}},
        {URL: {"kind": "stage"}},
        {URL: {"kind": "stage", "scope": "the gate", "extra": "x"}},
    ],
)
def test_the_ledger_refuses_a_malformed_pull_scope(scopes):
    op = {"op": "task_update", "id": "u1", "by": ENGINEER, "item": "tasks/t1", "fields": {"pull_scopes": scopes}}
    with pytest.raises(ValueError) as refused:
        ledger_tasks.check(op)
    assert str(refused.value) == (
        "pull_scopes must map pull request URLs to a kind, stage or prerequisite, and a nonempty scope"
    )
