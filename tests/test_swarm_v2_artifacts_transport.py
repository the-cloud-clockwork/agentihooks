import threading
from types import SimpleNamespace

import pytest

from scripts.swarm_v2.api import server
from scripts.swarm_v2.artifacts import remote
from scripts.swarm_v2.artifacts.base import ArtifactError
from scripts.swarm_v2.auth_context import GrantRefused
from scripts.swarm_v2.broadcast_bridge import grant_path
from tests.test_swarm_v2_artifacts_api import storage as storage


@pytest.fixture
def endpoint(storage, tmp_path):
    api, backend, authority = storage
    routes = server.Routes(SimpleNamespace(route=lambda *args: (404, {})), None)
    routes.artifacts = api
    http = server.serve(routes, "127.0.0.1", 0)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    path = grant_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("grant")
    spec = {
        "control_url": f"http://127.0.0.1:{http.server_port}",
        "registration": {"generation": 999, "task_id": "foreign"},
    }
    try:
        yield spec, tmp_path, backend, authority
    finally:
        http.shutdown()
        http.server_close()
        thread.join(3)


def test_worker_publishes_a_verified_checkpoint_over_authenticated_http(endpoint):
    spec, attempt, backend, _ = endpoint
    store = remote.worker_store(spec, attempt, {})
    assert store.scope.task_id == "task"
    assert store.scope.generation == 7
    data = b"checkpoint bytes" * 16384
    reference = store.put("checkpoint", data)
    assert store.get_range(reference) == data
    manifest = store.commit_manifest("checkpoint", ["checkpoint"])
    assert manifest["generation"] == 7
    assert manifest["execution_id"] == "execution"
    assert backend.keys("swarm/task/staging/") == []
    assert store.backend.kind == "object-store"


def test_worker_backend_rereads_renewed_grant_for_every_operation(endpoint):
    spec, attempt, _, authority = endpoint
    store = remote.worker_store(spec, attempt, {})
    grant_path(attempt).write_text("renewed-grant")
    assert store.backend.size("swarm/task/objects/missing") is None
    authority.authorize.assert_called_with("renewed-grant")


def test_worker_backend_surfaces_generation_refusal(endpoint):
    spec, attempt, _, authority = endpoint
    store = remote.worker_store(spec, attempt, {})
    authority.authorize.side_effect = GrantRefused("stale_generation", "superseded")
    with pytest.raises(ArtifactError, match="stale_generation"):
        store.backend.size("swarm/task/objects/missing")


def test_worker_backend_cannot_write_another_task(endpoint):
    spec, attempt, backend, _ = endpoint
    store = remote.worker_store(spec, attempt, {})
    with pytest.raises(ArtifactError, match="forbidden_scope"):
        store.backend.write("swarm/foreign/staging/execution/data", b"forbidden")
    assert backend.keys("") == []


def test_worker_control_url_can_end_with_a_slash(endpoint):
    spec, attempt, _, _ = endpoint
    spec["control_url"] += "/"
    assert remote.worker_store(spec, attempt, {}).scope.generation == 7


def test_worker_uses_the_configured_grant_file(endpoint):
    from scripts.swarm_v2.broadcast_bridge import GRANT_FILE

    spec, attempt, _, authority = endpoint
    credential = attempt / "separate-grant"
    credential.write_text("configured-grant")
    assert remote.worker_store(spec, attempt, {GRANT_FILE: str(credential)}).scope.generation == 7
    authority.authorize.assert_called_with("configured-grant")


def test_scope_handshake_authorizes_the_original_constructor_grant(endpoint):
    from pathlib import Path
    from unittest.mock import patch

    spec, attempt, _, authority = endpoint
    credential = grant_path(attempt)
    read_bytes = Path.read_bytes

    def renewed(path):
        data = read_bytes(path)
        if path == credential:
            credential.write_text("renewed-grant")
        return data

    with patch.object(Path, "read_bytes", renewed):
        assert remote.worker_store(spec, attempt, {}).scope.generation == 7
    authority.authorize.assert_called_once_with("grant")


def test_unconfigured_artifact_route_uses_the_execution_api():
    default = SimpleNamespace(route=lambda *args: (404, {"owner": "executions"}))
    routes = server.Routes(default, None)
    assert routes.route("POST", "/v2/artifacts/backend", "Bearer grant", {"operation": "scope"}) == (
        404,
        {"owner": "executions"},
    )


def test_configured_artifact_route_preserves_other_api_paths(storage):
    handler, _, _ = storage
    default = SimpleNamespace(route=lambda *args: (404, {"owner": "executions"}))
    routes = server.Routes(default, None)
    routes.artifacts = handler
    assert routes.route("GET", "/other", "Bearer grant", None) == (404, {"owner": "executions"})
