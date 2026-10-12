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
