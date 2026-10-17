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


@pytest.mark.parametrize("change,status", [("revoked", 401), ("generation", 409), ("expired", 409)])
def test_authority_change_during_storage_read_refuses_commit(storage, monkeypatch, change, status):
    api, backend, authority = storage
    size = backend.size

    def changed(key):
        found = size(key)
        if change == "revoked":
            authority.authorize.side_effect = GrantRefused("unauthenticated", "revoked")
        elif change == "generation":
            authority.current.return_value.generation = 8
        else:
            authority.current.return_value.lease_deadline_ms = 100
        return found

    monkeypatch.setattr(backend, "size", changed)
    key = "swarm/task/staging/execution/checkpoint"
    assert request(api, "write", key=key, data=encoded(b"checkpoint"))[0] == status
    assert backend.keys("") == []


def test_immutable_publication_keeps_a_concurrent_committed_record(storage, monkeypatch):
    api, backend, _ = storage
    key = "swarm/task/artifacts/checkpoint.json"
    first = json.dumps({"execution_id": "execution", "generation": 7, "sha256": "a" * 64, "size": 1}).encode()
    second = json.dumps({"execution_id": "execution", "generation": 7, "sha256": "b" * 64, "size": 1}).encode()
    size = backend.size

    def concurrent(subject):
        found = size(subject)
        if subject == key and found is None:
            backend.write(key, first)
        return found

    monkeypatch.setattr(backend, "size", concurrent)
    assert request(api, "write", key=key, data=encoded(second))[0] == 409
    assert backend.read(key, 0, len(first)) == first


def test_raw_removal_preserves_committed_manifest_references(storage):
    from dataclasses import replace

    from scripts.swarm_v2.artifacts.base import ArtifactStore

    api, backend, authority = storage
    registration = replace(authority.authorize.return_value, generation=7)
    store = ArtifactStore(backend, lambda token: registration, "grant")
    reference = store.put("checkpoint", b"retained")
    store.commit_manifest("checkpoint", ["checkpoint"])
    record = "swarm/task/artifacts/checkpoint.json"
    object_key = f"swarm/task/objects/{reference.sha256}"
    assert request(api, "remove", key=record)[0] == 403
    assert request(api, "remove", key=object_key)[0] == 403
    assert store.get_range(reference) == b"retained"


@pytest.mark.parametrize("category", ["objects", "artifacts"])
def test_final_artifact_deletion_is_refused_before_a_provider_takeover(storage, monkeypatch, category):
    api, backend, authority = storage
    data = b"retained"
    digest = hashlib.sha256(data).hexdigest()
    key = f"swarm/task/objects/{digest}" if category == "objects" else "swarm/task/artifacts/checkpoint.json"
    stored = (
        data
        if category == "objects"
        else json.dumps({"execution_id": "execution", "generation": 7, "sha256": digest, "size": len(data)}).encode()
    )
    backend.write(key, stored)
    remove = backend.remove

    def takeover(subject):
        authority.current.return_value.generation = 8
        remove(subject)

    provider = Mock(side_effect=takeover)
    monkeypatch.setattr(backend, "remove", provider)
    assert request(api, "remove", key=key)[0] == 403
    provider.assert_not_called()
    assert backend.read(key, 0, len(stored)) == stored


@pytest.mark.parametrize("operation", ["write", "read", "move"])
def test_artifact_outcomes_log_only_authenticated_scope_and_status(storage, capsys, operation):
    api, backend, authority = storage
    digest = hashlib.sha256(b"private conversation bytes").hexdigest()
    key = f"swarm/task/staging/execution/{digest}"
    backend.write(key, b"private conversation bytes")
    fields = {
        "write": {"key": key, "data": encoded(b"private conversation bytes")},
        "read": {"key": key, "start": 0, "length": 26},
        "move": {"key": key, "target": f"swarm/task/objects/{digest}"},
    }
    status, _ = request(api, operation, **fields[operation])
    assert status == 200
    assert json.loads(capsys.readouterr().out) == {
        "event": "checkpoint_storage",
        "backend": "object-store",
        "operation": operation,
        "execution_id": "execution",
        "generation": 7,
        "status": 200,
        "error_class": None,
    }


@pytest.mark.parametrize("error_class, status", [("unauthenticated", 401), ("stale_generation", 409)])
def test_artifact_refusals_log_no_grant_or_provider_error_message(storage, capsys, error_class, status):
    from scripts.swarm_v2.auth_context import GrantRefused

    api, _, authority = storage
    authority.authorize.side_effect = GrantRefused(error_class, "private provider response")
    assert request(api, "write", key="not public", data="not public")[0] == status
    assert json.loads(capsys.readouterr().out) == {
        "event": "checkpoint_storage",
        "backend": "object-store",
        "operation": "write",
        "execution_id": None,
        "generation": None,
        "status": status,
        "error_class": error_class,
    }


@pytest.mark.parametrize("body", [None, {"operation": "untrusted-operation"}, {"operation": "write"}])
def test_invalid_artifact_requests_emit_only_the_fixed_operation_label(storage, monkeypatch, body):
    from tests.public_output import BufferedOutput

    api, _, _ = storage
    output = BufferedOutput()
    monkeypatch.setattr("sys.stdout", output)
    assert api.route("POST", artifacts.PATH, "Bearer grant", body)[0] == 400
    assert json.loads(output.visible) == {
        "event": "checkpoint_storage",
        "backend": "object-store",
        "operation": "invalid_request",
        "execution_id": None,
        "generation": None,
        "status": 400,
        "error_class": "invalid_request",
    }


def test_authenticated_artifact_refusal_keeps_its_scope_in_the_immediate_event(storage, monkeypatch):
    from tests.public_output import BufferedOutput

    api, _, _ = storage
    output = BufferedOutput()
    monkeypatch.setattr("sys.stdout", output)
    assert request(api, "write", key="outside/task", data=encoded(b"private bytes"))[0] == 403
    assert json.loads(output.visible) == {
        "event": "checkpoint_storage",
        "backend": "object-store",
        "operation": "write",
        "execution_id": "execution",
        "generation": 7,
        "status": 403,
        "error_class": "forbidden_scope",
    }


def test_a_committed_checkpoint_pointer_is_recorded_with_its_execution_seat_and_time(storage):
    from scripts.swarm import lease

    _, backend, authority = storage
    seen = []
    api = artifacts.ArtifactsAPI(backend, authority, lambda *args: seen.append(args))
    document = json.dumps({"execution_id": "execution", "generation": 7}).encode()
    record = "swarm/task/artifacts/checkpoint.json"
    assert request(api, "write", key=record, data=encoded(document)) == (200, {})
    assert seen == []
    pointer = "swarm/task/pointers/0000000007/0000000001.json"
    assert request(api, "write", key=pointer, data=encoded(document)) == (200, {})
    assert seen == [("execution", "seat", lease.now_ms(authority.store))]
    assert request(api, "write", key="swarm/task/pointers/0000000008/0000000002.json", data=encoded(document))[0] == 403
    assert len(seen) == 1


def test_a_lost_checkpoint_reading_never_fails_the_committed_pointer(storage, capsys):
    from redis.exceptions import ConnectionError as RedisDown

    _, backend, authority = storage

    def down(*args):
        raise RedisDown("gone")

    api = artifacts.ArtifactsAPI(backend, authority, down)
    document = json.dumps({"execution_id": "execution", "generation": 7}).encode()
    pointer = "swarm/task/pointers/0000000007/0000000001.json"
    assert request(api, "write", key=pointer, data=encoded(document)) == (200, {})
    assert backend.size(pointer) == len(document)
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert {"event": "checkpoint_reading_lost", "error": "ConnectionError"} in events
