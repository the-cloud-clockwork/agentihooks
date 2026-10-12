import base64
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.swarm_v2.api import artifacts
from scripts.swarm_v2.artifacts.object_store import ObjectStoreBackend
from scripts.swarm_v2.auth_context import GrantRefused, Registration
from tests.test_swarm_v2_artifacts_remote import Objects


@pytest.fixture
def storage():
    registration = Registration(
        "grant", "swarm", "execution", 2, "seat", "task", "account", "brain", [], "issuer", "audience", "key", "now"
    )
    claim = SimpleNamespace(
        execution_id="execution", execution_generation=2, generation=7, state="active", lease_deadline_ms=200
    )
    authority = SimpleNamespace(
        authorize=Mock(return_value=registration),
        current=Mock(return_value=claim),
        controller=SimpleNamespace(require=Mock()),
        store=SimpleNamespace(redis=SimpleNamespace(time=lambda: (0, 100000))),
    )
    backend = ObjectStoreBackend(Objects(), "bucket", "checkpoints/")
    return artifacts.ArtifactsAPI(backend, authority), backend, authority


def request(api, operation, **fields):
    return api.route("POST", "/v2/artifacts/backend", "Bearer grant", {"operation": operation, **fields})


def encoded(data):
    return base64.b64encode(data).decode()


def test_registration_handshake_uses_authoritative_task_generation(storage):
    api, _, authority = storage
    status, body = request(api, "scope")
    assert status == 200
    assert body["registration"]["generation"] == 7
    assert body["registration"]["execution_id"] == "execution"
    assert body["registration"]["task_id"] == "task"
    authority.authorize.assert_called_once_with("grant")


def test_authenticated_backend_round_trip_keeps_checkpoint_bytes(storage):
    api, backend, _ = storage
    data = b"portable checkpoint"
    digest = hashlib.sha256(data).hexdigest()
    staging = f"swarm/task/staging/execution/{digest}"
    target = f"swarm/task/objects/{digest}"
    assert request(api, "write", key=staging, data=encoded(data)) == (200, {})
    assert request(api, "size", key=staging) == (200, {"size": 19})
    assert request(api, "read", key=staging, start=9, length=10) == (200, {"data": encoded(b"checkpoint")})
    assert request(api, "move", key=staging, target=target) == (200, {})
    assert request(api, "keys", key="swarm/task/objects/") == (200, {"keys": [target]})
    assert request(api, "remove", key=staging) == (200, {})
    assert backend.read(target, 0, 19) == data


@pytest.mark.parametrize(
    "key",
    [
        "swarm/other/staging/execution/blob",
        "other/task/staging/execution/blob",
        "swarm/task/../other/blob",
        "swarm/task/staging/other/blob",
        "swarm/task/pointers/0000000008/0000000001.json",
    ],
)
def test_worker_cannot_upload_outside_its_task_execution_and_generation(storage, key):
    api, backend, _ = storage
    assert request(api, "write", key=key, data=encoded(b"foreign"))[0] == 403
    assert backend.keys("") == []


def test_foreign_generation_document_cannot_be_promoted(storage):
    api, backend, _ = storage
    document = json.dumps({"execution_id": "execution", "generation": 8}).encode()
    key = "swarm/task/artifacts/checkpoint.json"
    assert request(api, "write", key=key, data=encoded(document))[0] == 403
    assert backend.size(key) is None


def test_current_worker_can_read_prior_generation_of_only_its_task(storage):
    api, backend, _ = storage
    key = "swarm/task/pointers/0000000006/0000000001.json"
    backend.write(key, b"previous")
    assert request(api, "read", key=key, start=0, length=8) == (200, {"data": encoded(b"previous")})
    assert request(api, "read", key="swarm/other/pointers/0000000006/0000000001.json", start=0, length=8)[0] == 403


def test_every_operation_reauthenticates_the_grant(storage):
    api, _, authority = storage
    assert request(api, "scope")[0] == 200
    authority.authorize.side_effect = GrantRefused("stale_generation", "superseded")
    status, body = request(api, "scope")
    assert status == 409
    assert body["error_class"] == "stale_generation"


def test_expired_task_lease_refuses_mutation(storage):
    api, backend, authority = storage
    authority.current.return_value.lease_deadline_ms = 100
    assert request(api, "write", key="swarm/task/staging/execution/blob", data=encoded(b"blob"))[0] == 409
    assert backend.keys("") == []
