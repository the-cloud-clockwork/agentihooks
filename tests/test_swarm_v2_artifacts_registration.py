import json
from unittest.mock import Mock

import pytest


@pytest.fixture
def registered(monkeypatch, tmp_path):
    from scripts.swarm_v2 import control_service
    from scripts.swarm_v2.artifacts import remote
    from scripts.swarm_v2.artifacts.object_store import ObjectStoreBackend
    from scripts.swarm_v2.auth_context import LaunchKey
    from scripts.swarm_v2.broadcast_bridge import grant_path
    from tests.test_swarm_v2_artifacts_remote import Objects
    from tests.test_swarm_v2_control_service import SLUG, _admitted, _store

    backend = ObjectStoreBackend(Objects(), "bucket", "checkpoints/")
    factory = Mock(return_value=backend)
    monkeypatch.setattr(remote, "controller_backend", factory)
    store = _store()
    service = control_service.ControlService(store, SLUG, LaunchKey("fixture", b"f" * 32), lambda: True)
    assert service.start()
    agent, token = _admitted(service, store)
    service.executions.register(token, {"execution_id": agent.execution_id, "generation": agent.generation})
    http = service.serve("127.0.0.1", 0)
    credential = grant_path(tmp_path)
    credential.parent.mkdir(parents=True)
    credential.write_text(token)
    spec = {"control_url": f"http://127.0.0.1:{http.server_port}"}
    try:
        yield remote, service, backend, spec, tmp_path, agent, factory
    finally:
        service.stop()


def test_deployed_controller_serves_registered_checkpoint_round_trip(registered):
    remote, _, backend, spec, attempt, agent, factory = registered
    store = remote.worker_store(spec, attempt, {})
    reference = store.put("checkpoint", b"retained through controller restart")
    assert store.get_range(reference) == b"retained through controller restart"
    assert store.scope.execution_id == agent.execution_id
    assert backend.keys(f"{store.scope.swarm_id}/{store.scope.task_id}/artifacts/")
    factory.assert_called_once()


def test_real_launch_authority_refuses_an_unregistered_worker(registered):
    from scripts.swarm_v2.artifacts.base import ArtifactError
    from scripts.swarm_v2.broadcast_bridge import grant_path

    remote, _, backend, spec, attempt, _, _ = registered
    grant_path(attempt).write_text("not-a-grant")
    with pytest.raises(ArtifactError, match="unauthenticated"):
        remote.worker_store(spec, attempt, {})
    assert backend.keys("") == []


def test_registered_worker_cannot_upload_a_foreign_task_generation(registered):
    from scripts.swarm_v2.artifacts.base import ArtifactError

    remote, _, backend, spec, attempt, _, _ = registered
    store = remote.worker_store(spec, attempt, {})
    scope = store.scope
    key = scope.key("pointers", str(scope.generation + 1), "1.json")
    data = json.dumps({"execution_id": scope.execution_id, "generation": scope.generation + 1}).encode()
    with pytest.raises(ArtifactError, match="forbidden_scope"):
        store.backend.write(key, data)
    assert backend.keys("") == []


def test_a_worker_checkpoint_pointer_becomes_the_controller_fleet_reading(registered):
    from scripts.events import state
    from scripts.swarm import lease, metrics_fleet
    from tests.test_swarm_v2_control_service import SLUG

    remote, service, _, spec, attempt, agent, _ = registered
    store = remote.worker_store(spec, attempt, {})
    scope = store.scope
    key = scope.key("pointers", f"{scope.generation:010d}", "0000000001.json")
    data = json.dumps({"execution_id": scope.execution_id, "generation": scope.generation}).encode()
    store.backend.write(key, data)
    redis_store = service.tasks.store
    saved = state.for_store(redis_store, metrics_fleet.CHECKPOINTS).get(SLUG)
    found = json.loads(saved.value)[agent.execution_id]["checkpoint"]
    assert (found["execution_id"], found["seat"]) == (agent.execution_id, agent.seat)
    assert 0 <= lease.now_ms(redis_store) - found["taken_at_ms"] < 60_000
