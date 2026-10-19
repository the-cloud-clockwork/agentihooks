import pytest

from scripts.swarm_ledger.api.errors import APIError
from tests.swarm_ledger.test_controller_hosted_ledger import (
    SLUG,
    _held_task,
    _hl,
    core,
    ledger_server,
)
from tests.swarm_ledger.test_controller_hosted_ledger import (
    deployed as deployed,
)
from tests.swarm_ledger.test_controller_hosted_ledger import (
    hosted as hosted,
)
from tests.swarm_ledger.test_controller_hosted_ledger import (
    store as store,
)


def _operation(deployed):
    claim = deployed.service.tasks.current("t1")
    return {
        "op": "task_update",
        "id": "worker-direct",
        "by": deployed.agent.name,
        "item": "tasks/t1",
        "fields": {"state": "pr"},
        "worker_claim": {
            "task_id": "t1",
            "execution_id": claim.execution_id,
            "execution_generation": claim.execution_generation,
            "task_generation": claim.generation,
            "spec_revision": _hl().spec_revision(_held_task()),
        },
    }


def test_hosted_apply_accepts_the_current_spec_even_with_an_old_task_guard(deployed):
    from scripts.swarm_ledger.api import mutations, schemas

    operation = _operation(deployed)
    payload = {"operation_id": "direct-current", "ops": [operation], "guards": {schemas.target(operation): "f" * 64}}

    reply = mutations.apply(ledger_server.api_services(), SLUG, "", payload)

    assert reply["applied"] == ["worker-direct"]
    assert reply["rejected"] == []
    assert _held_task()["state"] == "pr"


def test_hosted_worker_apply_uses_the_current_redis_adapter_after_module_loading(deployed):
    import importlib
    from unittest.mock import patch

    from scripts.swarm_ledger.api import mutations, worker_writes
    from tests.test_swarm_v2_control_service import _store

    operation = _operation(deployed)
    payload = {"operation_id": "adapter-recovery", "ops": [operation], "guards": {}}
    try:
        with patch("scripts.swarm.store.connect", return_value=_store()):
            importlib.reload(worker_writes)

        reply = mutations.apply(ledger_server.api_services(), SLUG, "", payload)

        assert reply["applied"] == ["worker-direct"]
        assert _held_task()["state"] == "pr"
    finally:
        importlib.reload(worker_writes)


@pytest.mark.parametrize(
    "field,value",
    [
        ("extra", True),
        ("task_id", None),
        ("task_id", ""),
        ("execution_id", None),
        ("execution_id", ""),
        ("execution_id", 1),
        ("task_id", 1),
        ("execution_generation", None),
        ("execution_generation", 0),
        ("execution_generation", "1"),
        ("task_generation", None),
        ("task_generation", 0),
        ("task_generation", "1"),
        ("spec_revision", None),
        ("spec_revision", "invalid"),
        ("spec_revision", 1),
    ],
)
def test_hosted_worker_claim_schema_refuses_malformed_pins(deployed, field, value):
    from scripts.swarm_ledger.api import schemas

    operation = _operation(deployed)
    if value is None:
        operation["worker_claim"].pop(field)
    else:
        operation["worker_claim"][field] = value
    payload = {"operation_id": "malformed-pin", "ops": [operation], "guards": {}}

    with pytest.raises(APIError) as raised:
        schemas.check_operations(payload, core, ("t1",))

    assert raised.value.status == 400
    assert _held_task()["state"] == "claimed"


@pytest.mark.parametrize("pin", [None, [], 1])
def test_hosted_worker_claim_must_be_an_object(deployed, pin):
    from scripts.swarm_ledger.api import schemas

    operation = _operation(deployed)
    operation["worker_claim"] = pin
    payload = {"operation_id": "invalid-object", "ops": [operation], "guards": {}}
    with pytest.raises(APIError) as raised:
        schemas.check_operations(payload, core, ("t1",))
    assert raised.value.status == 400


@pytest.mark.parametrize("field", ["task_id", "execution_id"])
def test_worker_claim_schema_accepts_single_character_identities(deployed, field):
    from scripts.swarm_ledger.api import schemas

    operation = _operation(deployed)
    operation["worker_claim"][field] = "e"
    payload = {"operation_id": "short-identity", "ops": [operation], "guards": {}}
    assert schemas.check_operations(payload, core, ("t1",)) == [operation]


@pytest.mark.parametrize(
    "change,message",
    [
        ("holder", "Stale worker claim"),
        ("target", "Wrong task claim"),
        ("spec", "Changed task specification"),
        ("missing", "Changed task specification"),
    ],
)
def test_worker_fence_records_the_refusal_without_changing_the_task(deployed, store, change, message):
    from scripts.swarm_ledger.api import worker_writes

    operation = _operation(deployed)
    doc = {"tasks": [_held_task()]}
    if change == "holder":
        operation["by"] = "another worker"
    elif change == "target":
        operation["item"] = "tasks/t2"
    elif change == "spec":
        operation["worker_claim"]["spec_revision"] = "f" * 64
    else:
        doc["tasks"] = []

    with pytest.raises(APIError) as raised:
        worker_writes.check(SLUG, doc, operation)

    code = "stale_generation" if change in ("holder", "target") else "revision_conflict"
    assert raised.value.status == 409
    assert raised.value.envelope() == {"error": {"code": code, "message": message}}
    key = store.key(SLUG, "hosted-worker-refusals")
    assert store.redis.hget(key, "worker-direct") == code
    assert 0 < store.redis.ttl(key) <= 86400
    assert _held_task()["state"] == "claimed"
