import json
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from scripts.hive import auth
from scripts.swarm.store import AgentRecord
from scripts.swarm_v2.auth_context import LaunchKey
from scripts.swarm_v2.control_service import ControlService
from scripts.swarm_v2.operator_auth import authenticator
from tests import sv2_kub05_cases as cases

pytestmark = pytest.mark.unit


@pytest.fixture
def pending():
    world = cases.World()
    with world.clocked():
        controller, _ = world.controller()
        assert controller.acquire()
        old = world.launch(controller, world.fx["first"])
        world.checkpoints.by_execution[old.execution_id] = []
        recovery = world.recovery(controller)
        recovery.handle(old.execution_id, "node_lost")
        slug = recovery.slug
        world.store.put_agent(slug, AgentRecord("coordinator", "master", ""))
        world.store.redis.hset(f"{auth.PREFIX}:member:fixture", "host", "fixture")
        token = auth.issue_agent(world.store.redis, "fixture", slug, "coordinator")
        service = ControlService(
            world.store,
            slug,
            LaunchKey("fixture", b"synthetic signing material" * 4),
            lambda: True,
            operator=authenticator(lambda _: "fixture operator credential"),
        )
        service.recover = SimpleNamespace(recovery=recovery)
        server = service.serve("127.0.0.1", 0)
        url = f"http://127.0.0.1:{server.server_port}/v1/executions/{old.execution_id}/restore-decision"
        yield SimpleNamespace(world=world, recovery=recovery, old=old, service=service, url=url, token=token)
        service.stop()
        controller.release()


def post(pending, token, body, method="POST"):
    request = urllib.request.Request(
        pending.url,
        json.dumps(body).encode(),
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_authenticated_master_releases_pending_anton_recovery(pending):
    status, result = post(pending, pending.token, {"agent": "coordinator", "choice": "fresh"})
    assert status == 200
    assert result["execution_id"] == pending.old.execution_id
    assert result["mode"] == "fresh"
    assert result["replacement"] == pending.world.occupant().execution_id
    assert pending.world.occupant().generation == 2
    assert pending.recovery.decision(pending.old.execution_id).mode == "fresh"


@pytest.mark.parametrize("name,lane", [("master lookalike", "eng"), ("operator", "eng")])
def test_authenticated_worker_display_labels_cannot_authorize_recovery(pending, name, lane):
    slug = pending.recovery.slug
    pending.world.store.put_agent(slug, AgentRecord(name, lane, "task"))
    token = auth.issue_agent(pending.world.store.redis, "fixture", slug, name)
    before = pending.world.protected()
    status, result = post(pending, token, {"agent": name, "choice": "fresh"})
    assert status == 403
    assert result["error_class"] == "forbidden_scope"
    assert pending.world.protected() == before


def test_forged_master_display_label_is_unauthenticated(pending):
    before = pending.world.protected()
    status, result = post(pending, pending.token, {"agent": "operator", "choice": "fresh"})
    assert status == 401
    assert result["error_class"] == "unauthenticated"
    assert pending.world.protected() == before


def test_operator_can_choose_fresh(pending):
    status, result = post(pending, "fixture operator credential", {"choice": "fresh"})
    assert status == 200
    assert result["mode"] == "fresh"


def test_master_resume_without_checkpoint_keeps_recovery_pending(pending):
    status, result = post(pending, pending.token, {"agent": "coordinator", "choice": "resume"})
    assert status == 409
    assert result["error_class"] == "recovery_refused"
    assert pending.recovery.decision(pending.old.execution_id).mode == "recovery_pending"


def test_master_resume_with_compatible_checkpoint_replaces_the_attempt(pending):
    pending.world.checkpoints.by_execution[pending.old.execution_id] = pending.world.fx["first"]["checkpoints"]
    status, result = post(pending, pending.token, {"agent": "coordinator", "choice": "resume"})
    assert status == 200
    assert result["mode"] == "resume"
    assert result["checkpoint"]
    assert result["replacement"] == pending.world.occupant().execution_id
