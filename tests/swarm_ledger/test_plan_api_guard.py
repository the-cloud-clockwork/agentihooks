import pytest

from scripts.swarm_ledger import ledger_core
from scripts.swarm_ledger.api import schemas
from scripts.swarm_ledger.api.errors import APIError
from tests.swarm_ledger import test_api_v1
from tests.swarm_ledger.plan_slices import anchored
from tests.swarm_ledger.test_ledger_authority import SLUG, core, ledger

authority_live = test_api_v1.authority_live
live = test_api_v1.live


@pytest.mark.parametrize("guard", [True, False])
def test_task_update_transport_accepts_boolean_missing_range_guards(guard):
    operation = {
        "op": "task_update",
        "id": "range-guard",
        "by": "planner",
        "item": "tasks/t1",
        "fields": {"plan_slice": "first"},
        "if_plan_lines_missing": guard,
    }
    payload = {"operation_id": "request", "ops": [operation], "guards": {}}
    assert schemas.check_operations(payload, ledger_core, ("t1",)) == [operation]


@pytest.mark.parametrize("guard", ["yes", 1, None, []])
def test_task_update_transport_rejects_nonboolean_missing_range_guards(guard):
    operation = {
        "op": "task_update",
        "id": "range-guard",
        "by": "planner",
        "item": "tasks/t1",
        "fields": {"plan_slice": "first"},
        "if_plan_lines_missing": guard,
    }
    payload = {"operation_id": "request", "ops": [operation], "guards": {}}
    with pytest.raises(APIError) as error:
        schemas.check_operations(payload, ledger_core, ("t1",))
    assert error.value.code == "schema_invalid"
    assert str(error.value) == "Operation task_update does not match its schema at field if_plan_lines_missing"


def test_guarded_slice_assignment_crosses_the_versioned_request_boundary(live):
    ledger.request(SLUG, [{"op": "join", "id": "boss-join", "by": "boss", "role": "orchestrator"}], service=True)
    ledger.request(
        SLUG,
        [
            {
                "op": "task_add",
                "id": "create",
                "by": "boss",
                "task": "t1",
                "title": "Proof",
                "lane": "eng",
                "phase": "p1",
            }
        ],
        service=True,
    )
    anchored(SLUG, "first", "second", core=core)
    cases = [(True, "first", "first", "2-3"), (True, "absent", "first", "2-3"), (False, "second", "second", "4-5")]
    for number, (guard, name, expected_name, expected_lines) in enumerate(cases):
        expected = test_api_v1.request(live, "GET", "tasks/t1")[1]["revision"]
        operation = {
            "op": "task_update",
            "id": f"range-{number}",
            "by": "boss",
            "item": "tasks/t1",
            "fields": {"plan_slice": name},
            "if_plan_lines_missing": guard,
        }
        status, reply = test_api_v1.request(
            live, "POST", "operations", {"ops": [operation], "guards": {"tasks/t1": expected}}
        )
        assert status == 200
        assert reply["rejected"] == []
        row = test_api_v1.request(live, "GET", "tasks/t1")[1]["data"]
        assert row["plan_slice"] == expected_name
        assert row["plan_lines"] == expected_lines
