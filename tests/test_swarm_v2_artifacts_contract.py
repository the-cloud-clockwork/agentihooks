import base64
import hashlib
import json

import pytest

import scripts.swarm_v2.api.artifacts as api
from scripts.swarm_v2.auth_context import GrantRefused
from tests.test_swarm_v2_artifacts_api import encoded, request
from tests.test_swarm_v2_artifacts_api import storage as storage


def refusal(error, message):
    return {"error_class": error, "operation_id": "unknown", "retry": "new_request", "message": message}


@pytest.mark.parametrize("method,path", [("GET", "/v2/artifacts/backend"), ("POST", "/other")])
def test_route_refuses_other_endpoints(storage, method, path):
    handler, _, _ = storage
    assert handler.route(method, path, "Bearer grant", {}) == (
        404,
        {"error_class": "invalid_request", "message": "no such artifact endpoint"},
    )


def test_route_requires_bearer_authentication(storage):
    handler, _, _ = storage
    assert handler.route("POST", "/v2/artifacts/backend", "", {}) == (
        401,
        {"error_class": "unauthenticated", "message": "a bearer credential is required"},
    )


@pytest.mark.parametrize("body", [None, [], {}, {"operation": "unknown"}])
def test_operation_schema_rejects_unknown_requests(storage, body):
    handler, _, _ = storage
    assert handler.route("POST", "/v2/artifacts/backend", "Bearer grant", body) == (
        400,
        refusal("invalid_request", "unknown artifact operation"),
    )


@pytest.mark.parametrize(
    "body", [{"operation": "scope", "key": "foreign"}, {"operation": "read", "key": "swarm/task/objects/blob"}]
)
def test_operation_schema_requires_exact_fields(storage, body):
    handler, _, _ = storage
    assert handler.route("POST", "/v2/artifacts/backend", "Bearer grant", body) == (
        400,
        refusal("invalid_request", "invalid artifact operation fields"),
    )


@pytest.mark.parametrize(
    "key,message",
    [
        (None, "artifact key is outside the registered task"),
        ("swarm/foreign/objects/blob", "artifact key is outside the registered task"),
        ("swarm/task/objects//blob", "artifact key is not canonical"),
        ("swarm/task/objects/./blob", "artifact key is not canonical"),
        ("swarm/task/objects/../blob", "artifact key is not canonical"),
        ("swarm/task/objects/back\\slash", "artifact key is not canonical"),
        ("swarm/task/objects", "artifact key names no object"),
    ],
)
def test_key_refusals_have_stable_scope_errors(storage, key, message):
    handler, _, _ = storage
    assert request(handler, "size", key=key) == (403, refusal("forbidden_scope", message))


@pytest.mark.parametrize("key", ["swarm/task/staging/foreign/blob", "swarm/task/staging/execution/nested/blob"])
def test_staging_is_execution_scoped(storage, key):
    handler, _, _ = storage
    assert request(handler, "remove", key=key) == (
        403,
        refusal("forbidden_scope", "staging key is outside the registered execution"),
    )


@pytest.mark.parametrize(
    "key",
    [
        "swarm/task/artifacts/record.json",
        "swarm/task/manifests/name/7.json",
        "swarm/task/pointers/0000000007/0000000001.json",
    ],
)
def test_each_current_generation_document_can_be_published(storage, key):
    handler, backend, _ = storage
    data = b'{"execution_id":"execution","generation":7}'
    assert request(handler, "write", key=key, data=encoded(data)) == (200, {})
    assert backend.read(key, 0, len(data)) == data
    assert request(handler, "write", key=key, data=encoded(data)) == (200, {})


@pytest.mark.parametrize(
    "document", [[], {}, {"execution_id": "foreign", "generation": 7}, {"execution_id": "execution", "generation": 8}]
)
def test_documents_require_both_generation_and_execution(storage, document):
    handler, _, _ = storage
    assert request(
        handler, "write", key="swarm/task/artifacts/record.json", data=encoded(json.dumps(document).encode())
    ) == (403, refusal("forbidden_scope", "artifact document is outside the registered task generation"))


@pytest.mark.parametrize("generation", ["8", "seven", "7.0", "٧"])
def test_manifest_and_pointer_keys_require_ascii_current_generation(storage, generation):
    handler, _, _ = storage
    data = encoded(b'{"execution_id":"execution","generation":7}')
    for key in (f"swarm/task/manifests/name/{generation}.json", f"swarm/task/pointers/{generation}/1.json"):
        assert request(handler, "write", key=key, data=data) == (
            403,
            refusal("forbidden_scope", "artifact key is outside the registered task generation"),
        )


def test_content_address_must_match_uploaded_bytes(storage):
    handler, _, _ = storage
    assert request(handler, "write", key="swarm/task/objects/" + "0" * 64, data=encoded(b"different")) == (
        403,
        refusal("forbidden_scope", "artifact object does not match its content address"),
    )


@pytest.mark.parametrize(
    "key", ["swarm/task/foreign/name.json", "swarm/task/artifacts/name", "swarm/task/objects/nested/blob"]
)
def test_only_artifact_document_categories_are_writable(storage, key):
    handler, _, _ = storage
    assert request(handler, "write", key=key, data=encoded(b"{}")) == (
        403,
        refusal("forbidden_scope", "artifact key is not writable"),
    )


def test_upload_base64_is_strict(storage):
    handler, _, _ = storage
    assert request(handler, "write", key="swarm/task/staging/execution/blob", data="%%") == (
        400,
        {"error_class": "invalid_request", "message": "invalid artifact request"},
    )


@pytest.mark.parametrize("start,length", [(True, 0), (0, False), (-1, 0), (0, -1), (0, 16777217)])
def test_read_ranges_require_bounded_nonnegative_integers(storage, start, length):
    handler, _, _ = storage
    assert request(handler, "read", key="swarm/task/objects/blob", start=start, length=length) == (
        400,
        refusal("invalid_request", "invalid artifact read range"),
    )


@pytest.mark.parametrize("start,length", [(0, 4), (3, 1)])
def test_reads_cannot_cross_the_object_boundary(storage, start, length):
    handler, backend, _ = storage
    backend.write("swarm/task/objects/blob", b"abc")
    assert request(handler, "read", key="swarm/task/objects/blob", start=start, length=length) == (
        400,
        refusal("invalid_request", "artifact read range is outside the object"),
    )


def test_absent_object_and_zero_length_reads(storage):
    handler, backend, _ = storage
    key = "swarm/task/objects/blob"
    assert request(handler, "read", key=key, start=0, length=0) == (
        400,
        refusal("invalid_request", "artifact read range is outside the object"),
    )
    backend.write(key, b"abc")
    assert request(handler, "read", key=key, start=3, length=0) == (200, {"data": ""})
    assert request(handler, "keys", key="swarm/task/") == (200, {"keys": [key]})


def test_upload_and_read_limits_include_the_exact_boundary(storage):
    handler, _, _ = storage
    data = b"a" * 16777216
    key = "swarm/task/staging/execution/large"
    assert request(handler, "write", key=key, data=encoded(data)) == (200, {})
    assert request(handler, "read", key=key, start=0, length=16777216) == (200, {"data": encoded(data)})
    assert request(handler, "write", key=key, data=encoded(data + b"a")) == (
        400,
        refusal("invalid_request", "artifact upload exceeds the size limit"),
    )


@pytest.mark.parametrize("change", ["missing", "execution", "execution_generation"])
def test_scope_requires_the_registered_execution_claim(storage, change):
    handler, _, authority = storage
    if change == "missing":
        authority.current.return_value = None
    else:
        setattr(
            authority.current.return_value,
            "execution_id" if change == "execution" else "execution_generation",
            "foreign",
        )
    assert request(handler, "scope") == (
        409,
        refusal("stale_generation", "artifact grant does not hold this task generation"),
    )
    authority.current.assert_called_with("task")


def test_controller_and_task_leases_guard_only_mutations(storage):
    from scripts.swarm.store import SwarmError

    handler, _, authority = storage
    authority.controller.require.side_effect = SwarmError("replaced")
    assert request(handler, "scope")[0] == 200
    assert request(handler, "write", key="swarm/task/staging/execution/blob", data=encoded(b"data")) == (
        409,
        refusal("stale_generation", "artifact controller lease is no longer active"),
    )
    authority.controller.require.side_effect = None
    authority.current.return_value.state = "released"
    assert request(handler, "size", key="swarm/task/objects/blob") == (200, {"size": None})
    assert request(handler, "write", key="swarm/task/staging/execution/blob", data=encoded(b"data")) == (
        409,
        refusal("stale_generation", "artifact task lease is no longer active"),
    )


def test_each_fence_uses_the_received_grant(storage, monkeypatch):
    handler, backend, authority = storage
    registration = authority.authorize.return_value

    def authorize(token):
        if token != "grant":
            raise GrantRefused("unauthenticated", "wrong credential")
        return registration

    authority.authorize.side_effect = authorize
    size = backend.size

    def generation_changed(key):
        result = size(key)
        authority.current.return_value.generation = 8
        return result

    monkeypatch.setattr(backend, "size", generation_changed)
    assert request(handler, "write", key="swarm/task/staging/execution/blob", data=encoded(b"data")) == (
        409,
        refusal("stale_generation", "artifact task generation changed during the request"),
    )


def test_same_immutable_content_is_replayed_but_different_content_conflicts(storage):
    handler, _, _ = storage
    key = "swarm/task/staging/execution/blob"
    assert request(handler, "write", key=key, data=encoded(b"first")) == (200, {})
    assert request(handler, "write", key=key, data=encoded(b"first")) == (200, {})
    assert request(handler, "write", key=key, data=encoded(b"other")) == (
        409,
        refusal("operation_conflict", "committed artifact content is immutable"),
    )


def test_object_content_address_is_accepted(storage):
    _, backend, authority = storage
    handler = api.ArtifactsAPI(backend, authority)
    data = b"content"
    key = "swarm/task/objects/" + hashlib.sha256(data).hexdigest()
    assert request(handler, "write", key=key, data=base64.b64encode(data).decode()) == (200, {})
    assert backend.read(key, 0, len(data)) == data


def test_move_target_requires_a_complete_object_key(storage):
    handler, backend, _ = storage
    data = b"target"
    staging = "swarm/task/staging/execution/target"
    backend.write(staging, data)
    assert request(handler, "move", key=staging, target="swarm/task/objects") == (
        403,
        refusal("forbidden_scope", "artifact key names no object"),
    )
    assert backend.read(staging, 0, len(data)) == data
