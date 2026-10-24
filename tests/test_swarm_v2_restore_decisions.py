import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from scripts.hive import auth
from scripts.swarm.store import AgentRecord
from scripts.swarm_v2.api.recovery import RecoveryAPI
from scripts.swarm_v2.auth_context import LaunchKey
from scripts.swarm_v2.control_service import ControlService
from scripts.swarm_v2.operator_auth import authenticator
from scripts.swarm_v2.restore_decisions import decide
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
            operator=authenticator(lambda requested: "fixture operator credential" if requested == slug else None),
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
    assert result["message"] == "only the master or operator can choose resume or fresh"
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


@pytest.mark.parametrize(
    "choice,reason",
    [
        ("native", "no complete compatible native checkpoint to resume from"),
        ("context", "no complete compatible checkpoint to resume from"),
    ],
)
def test_master_native_or_context_without_a_checkpoint_is_refused_and_keeps_waiting(pending, choice, reason):
    before = pending.world.protected()
    status, result = post(pending, pending.token, {"agent": "coordinator", "choice": choice})
    assert (status, result["error_class"], result["message"]) == (409, "recovery_refused", reason)
    assert pending.recovery.decision(pending.old.execution_id).mode == "recovery_pending"
    assert pending.world.protected() == before


def test_master_context_with_a_checkpoint_replaces_the_attempt(pending):
    pending.world.checkpoints.by_execution[pending.old.execution_id] = pending.world.fx["first"]["checkpoints"]
    status, result = post(pending, pending.token, {"agent": "coordinator", "choice": "context"})
    assert status == 200
    assert result["mode"] == "context"
    assert result["checkpoint"]
    assert result["replacement"] == pending.world.occupant().execution_id


def test_a_replayed_context_decision_admits_no_second_replacement(pending):
    pending.world.checkpoints.by_execution[pending.old.execution_id] = pending.world.fx["first"]["checkpoints"]
    first = post(pending, pending.token, {"agent": "coordinator", "choice": "context"})
    occupant = pending.world.occupant()
    again = post(pending, pending.token, {"agent": "coordinator", "choice": "context"})
    assert again == first
    assert pending.world.occupant() == occupant


def test_a_master_lists_verified_checkpoints_for_a_recovery(pending):
    entries = pending.world.fx["first"]["checkpoints"]
    pending.world.checkpoints.by_execution[pending.old.execution_id] = entries
    code, answer = pending.service.recovery_api.route(
        "POST",
        f"/v1/executions/{pending.old.execution_id}/checkpoints",
        "Bearer " + pending.token,
        {"agent": "coordinator"},
    )
    assert code == 200
    assert answer["checkpoints"] == entries


def test_the_controller_requires_a_bound_worker_before_confirming_recovery(pending):
    decision = pending.recovery.request(pending.old.execution_id, "fresh")
    path = f"/v1/executions/{decision.replacement}/restore-outcome"
    api = pending.service.recovery_api
    api.worker = lambda token: SimpleNamespace(execution_id=decision.replacement) if token == "worker fixture" else None
    body = {"recovery": "fresh", "checkpoint_id": "", "watermark": 4096, "session_id": "", "command": []}
    before = pending.world.protected()
    status, refused = api.route("POST", path, "Bearer unbound fixture", body)
    assert (status, refused["error_class"], refused["message"]) == (
        403,
        "forbidden_scope",
        "restore outcome is not bound to this execution",
    )
    assert pending.world.protected() == before
    assert pending.recovery.outcome(decision.replacement) is None
    status, confirmed = api.route("POST", path, "Bearer worker fixture", body)
    assert status == 200
    assert confirmed == {**body, "status": "verified", "archive_watermark": 4096}
    assert pending.recovery.restore_outcomes_by_type() == {"native": 0, "reconstructed": 0, "fresh": 1}
    assert api.route("POST", path, "Bearer worker fixture", body) == (200, confirmed)
    refused = {**body, "recovery": "native", "session_id": "fresh-session"}
    assert api.route("POST", path, "Bearer worker fixture", refused)[0] == 409
    assert pending.recovery.outcome(decision.replacement) == confirmed


@pytest.mark.parametrize("endpoint", ["checkpoints", "restore-outcome"])
def test_unavailable_controller_refuses_checkpoint_and_outcome_endpoints(pending, endpoint):
    api = pending.service.recovery_api
    api.worker = lambda token: SimpleNamespace(execution_id=pending.old.execution_id)
    pending.service.recover = None
    status, result = api.route(
        "POST",
        f"/v1/executions/{pending.old.execution_id}/{endpoint}",
        "Bearer " + pending.token,
        {"agent": "coordinator"},
    )
    assert (status, result["error_class"], result["message"]) == (
        503,
        "dependency_unavailable",
        "controller recovery is unavailable",
    )


def test_checkpoint_fetch_requires_a_verified_listing_and_preserves_identity(pending):
    api = pending.service.recovery_api
    old = pending.old.execution_id
    calls = []
    checkpoints = SimpleNamespace(
        list=lambda execution: calls.append(("list", execution)) or [{"checkpoint_id": "ckp-verified"}],
        fetch=lambda execution, checkpoint: (
            calls.append(("fetch", execution, checkpoint))
            or {"checkpoint_id": checkpoint, "authority": {"execution_id": execution}}
        ),
    )
    pending.recovery.checkpoints = checkpoints
    path = f"/v1/executions/{old}/checkpoints"
    assert api.route(
        "POST", path, "Bearer " + pending.token, {"agent": "coordinator", "checkpoint_id": "ckp-verified"}
    ) == (200, {"checkpoint_id": "ckp-verified", "authority": {"execution_id": old}})
    status, refused = api.route(
        "POST", path, "Bearer " + pending.token, {"agent": "coordinator", "checkpoint_id": "unverified"}
    )
    assert (status, refused["error_class"], refused["message"]) == (
        409,
        "recovery_refused",
        "checkpoint is not verified for this execution",
    )
    assert calls == [("list", old), ("fetch", old, "ckp-verified"), ("list", old)]


def test_outcome_from_another_execution_is_refused_before_any_record(pending):
    api = pending.service.recovery_api
    api.worker = lambda token: SimpleNamespace(execution_id="another")
    before = pending.world.protected()
    status, refused = api.route(
        "POST", f"/v1/executions/{pending.old.execution_id}/restore-outcome", "Bearer worker fixture", {}
    )
    assert (status, refused["error_class"], refused["message"]) == (
        403,
        "forbidden_scope",
        "restore outcome is not bound to this execution",
    )
    assert pending.world.protected() == before


def test_controller_recovery_authenticator_preserves_swarm_and_grant(pending, monkeypatch):
    calls = []
    registration = SimpleNamespace(execution_id=pending.old.execution_id)
    monkeypatch.setattr(
        pending.service.grants, "bound", lambda slug, token: calls.append((slug, token)) or registration
    )
    assert pending.service.recovery_api.worker("worker fixture") is registration
    assert calls == [(pending.recovery.slug, "worker fixture")]


def test_a_delayed_restore_receipt_cannot_overwrite_a_newer_attempt(pending):
    from dataclasses import replace

    decision = pending.recovery.request(pending.old.execution_id, "fresh")
    successor = pending.world.store.execution(pending.recovery.slug, decision.replacement)
    newer = pending.recovery.controller.admit(replace(successor, execution_id="", generation=0), successor.execution_id)
    body = {"recovery": "fresh", "checkpoint_id": "", "watermark": 4096, "session_id": "", "command": []}
    api = pending.service.recovery_api
    api.worker = lambda token: SimpleNamespace(execution_id=successor.execution_id)
    path = f"/v1/executions/{successor.execution_id}/restore-outcome"
    before = pending.world.protected()
    status, refused = api.route("POST", path, "Bearer delayed fixture", body)
    assert (status, refused["error_class"], refused["message"]) == (
        409,
        "recovery_refused",
        "restore outcome is for a superseded execution",
    )
    assert pending.world.protected() == before
    assert pending.world.occupant().execution_id == newer.execution_id
    assert pending.recovery.outcome(successor.execution_id) is None


@pytest.mark.parametrize("choice", ["fresh", "resume"])
def test_restore_command_delivers_master_decision_to_anton(pending, monkeypatch, capsys, choice):
    from scripts.gates import Who
    from scripts.swarm import cli, resume
    from scripts.swarm_ledger import ledger

    slug = pending.recovery.slug
    pending.world.store.update(slug, api_url=pending.url.split("/v1/")[0])
    monkeypatch.setattr(cli.Who, "from_env", lambda: Who("coordinator", slug, "master"))
    monkeypatch.delenv("AGENTIHOOKS_LEDGER_AGENT_TOKEN", raising=False)
    minted = []
    monkeypatch.setattr(ledger, "launch_token", lambda slug, name: minted.append((slug, name)) or pending.token)
    monkeypatch.setattr(
        resume, "decide", lambda *args: pytest.fail("Anton decisions must not reach workstation recovery")
    )
    if choice == "resume":
        pending.world.checkpoints.by_execution[pending.old.execution_id] = pending.world.fx["first"]["checkpoints"]
    args = SimpleNamespace(slug=slug, name="operator", agent=pending.old.name, choice=choice)
    cli.cmd_restore_decision(pending.world.store, args)
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == choice
    assert result["execution_id"] == pending.old.execution_id
    assert pending.recovery.decision(pending.old.execution_id).mode == choice
    assert minted == [(slug, "coordinator")]


def test_restore_command_cannot_gain_operator_role_from_its_name(pending, monkeypatch):
    from scripts.gates import Who
    from scripts.swarm import cli
    from scripts.swarm.store import SwarmError

    slug = pending.recovery.slug
    name = "worker"
    pending.world.store.put_agent(slug, AgentRecord(name, "eng", "task"))
    token = auth.issue_agent(pending.world.store.redis, "fixture", slug, name)
    pending.world.store.update(slug, api_url=pending.url.split("/v1/")[0])
    monkeypatch.setattr(cli.Who, "from_env", lambda: Who(name, slug, "eng"))
    monkeypatch.setenv("AGENTIHOOKS_LEDGER_AGENT_TOKEN", token)
    args = SimpleNamespace(slug=slug, name="operator", agent=pending.old.name, choice="fresh")
    before = pending.world.protected()
    with pytest.raises(SwarmError, match="master or operator"):
        cli.cmd_restore_decision(pending.world.store, args)
    assert pending.world.protected() == before


def test_overlapping_restore_decisions_accept_only_the_first_choice(pending, monkeypatch):
    pending.world.checkpoints.by_execution[pending.old.execution_id] = pending.world.fx["first"]["checkpoints"]
    read, authorized, released, reread = Event(), Event(), Event(), Event()
    decide, authorize = pending.recovery.decision, pending.service.recovery_api._authorize
    reads, callers = [], []

    def stalled(execution_id):
        known = decide(execution_id)
        reads.append(execution_id)
        if len(reads) == 1:
            read.set()
            assert released.wait(5)
        else:
            reread.set()
        return known

    def arriving(token, name):
        authorize(token, name)
        callers.append(name)
        if len(callers) == 2:
            authorized.set()

    monkeypatch.setattr(pending.recovery, "decision", stalled)
    monkeypatch.setattr(pending.service.recovery_api, "_authorize", arriving)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(post, pending, pending.token, {"agent": "coordinator", "choice": "fresh"})
        assert read.wait(5)
        second = pool.submit(post, pending, pending.token, {"agent": "coordinator", "choice": "resume"})
        assert authorized.wait(5)
        reread.wait(1)
        released.set()
        first_status, first_result = first.result(timeout=5)
        second_status, second_result = second.result(timeout=5)
    assert first_status == 200
    assert first_result["mode"] == "fresh"
    assert second_status == 409
    assert second_result["message"] == "no recovery decision is pending for this attempt"
    assert pending.recovery.decision(pending.old.execution_id).mode == "fresh"


def test_invalid_decision_requests_write_nothing(pending):
    api = pending.service.recovery_api
    before = pending.world.protected()
    authorization = f"Bearer {pending.token}"
    cases = [
        ("GET", pending.url.split("/v1/")[1], authorization, {}, 404, "no such recovery endpoint"),
        ("POST", "/v1/executions/unknown/other", authorization, {}, 404, "no such recovery endpoint"),
        ("POST", None, authorization, None, 400, "a recovery decision needs an object"),
        ("POST", None, "", {}, 401, "a bearer credential is required"),
        (
            "POST",
            None,
            authorization,
            {"agent": "coordinator", "choice": "invalid"},
            400,
            "a recovery decision is native, context, fresh or resume",
        ),
        (
            "POST",
            None,
            authorization,
            {"agent": 5, "choice": "fresh"},
            401,
            "the credential is not bound to this swarm and caller",
        ),
    ]
    for method, path, header, body, expected, message in cases:
        status, result = api.route(method, path or "/v1/" + pending.url.split("/v1/")[1], header, body)
        assert (status, result["message"]) == (expected, message)
        assert (
            result["error_class"] == {400: "invalid_request", 401: "unauthenticated", 404: "invalid_request"}[expected]
        )
    assert pending.world.protected() == before


def test_missing_recovery_and_unavailable_store_are_reported(pending, monkeypatch):
    from redis.exceptions import RedisError

    slug = pending.recovery.slug
    api = RecoveryAPI(pending.world.store, slug, lambda: None)
    status, result = api.route(
        "POST",
        "/v1/executions/attempt/restore-decision",
        f"Bearer {pending.token}",
        {"agent": "coordinator", "choice": "fresh"},
    )
    assert (status, result["message"]) == (503, "controller recovery is unavailable")
    assert result["error_class"] == "dependency_unavailable"

    def unavailable(*args):
        raise RedisError("synthetic outage")

    monkeypatch.setattr(pending.world.store, "agents", unavailable)
    status, result = api.route(
        "POST",
        "/v1/executions/attempt/restore-decision",
        f"Bearer {pending.token}",
        {"agent": "coordinator", "choice": "fresh"},
    )
    assert (status, result["message"]) == (503, "the recovery store is unavailable")
    assert result["error_class"] == "dependency_unavailable"


def test_finished_or_removed_master_cannot_decide_recovery(pending):
    slug = pending.recovery.slug
    pending.world.store.put_agent(slug, AgentRecord("coordinator", "master", "", state="finished"))
    status, result = post(pending, pending.token, {"agent": "coordinator", "choice": "fresh"})
    assert (status, result["error_class"]) == (403, "forbidden_scope")
    pending.world.store.drop_agent(slug, "coordinator")
    status, result = post(pending, pending.token, {"agent": "coordinator", "choice": "fresh"})
    assert (status, result["error_class"]) == (403, "forbidden_scope")
    pending.world.store.put_agent(slug, AgentRecord("coordinator", "master", ""))
    pending.world.store.redis.delete(f"{auth.PREFIX}:member:fixture")
    status, result = post(pending, pending.token, {"agent": "coordinator", "choice": "fresh"})
    assert (status, result["error_class"]) == (401, "unauthenticated")


def test_command_refuses_missing_controller_or_operator_credential(pending, monkeypatch):
    from scripts.gates import Who
    from scripts.swarm.store import SwarmError
    from scripts.swarm_v2 import control_service

    slug = pending.recovery.slug
    with pytest.raises(SwarmError, match="^the Anton controller API address is unavailable$"):
        decide(pending.world.store, slug, pending.old, "fresh", Who())
    pending.world.store.update(slug, api_url=pending.url.split("/v1/")[0])
    monkeypatch.setattr(control_service, "page_credential", lambda _: None)
    with pytest.raises(SwarmError, match="^the restore decision needs a master or operator credential$"):
        decide(pending.world.store, slug, pending.old, "fresh", Who())
    with pytest.raises(SwarmError, match="^the restore decision needs a master or operator credential$"):
        decide(pending.world.store, slug, pending.old, "fresh", Who("worker"))


def test_operator_command_reaches_anton_and_connection_errors_are_named(pending, monkeypatch):
    from scripts.gates import Who
    from scripts.swarm.store import SwarmError
    from scripts.swarm_v2 import control_service

    slug = pending.recovery.slug
    pending.world.store.update(slug, api_url=pending.url.split("/v1/")[0] + "/")
    monkeypatch.setattr(
        control_service,
        "page_credential",
        lambda requested: "fixture operator credential" if requested == slug else None,
    )
    result = decide(pending.world.store, slug, pending.old, "fresh", Who())
    assert result["mode"] == "fresh"

    def unreachable(*args, **kwargs):
        raise TimeoutError("synthetic timeout")

    monkeypatch.setattr(urllib.request, "urlopen", unreachable)
    with pytest.raises(SwarmError, match="controller could not be reached: TimeoutError"):
        decide(pending.world.store, slug, pending.old, "fresh", Who())


def test_routes_dispatch_recovery_and_preserve_sibling_endpoints(pending):
    from scripts.swarm_v2.api.server import Routes

    routes = Routes(
        pending.service.executions,
        None,
        health=pending.service.health_api,
        recovery=pending.service.recovery_api,
    )
    path = "/v1/" + pending.url.split("/v1/")[1]
    status, result = routes.route("POST", path, f"Bearer {pending.token}", {"agent": "coordinator", "choice": "fresh"})
    assert (status, result["mode"]) == (200, "fresh")
    status, result = routes.route("GET", "/v2/health/live", "", {})
    assert status == 200
    without_recovery = Routes(pending.service.executions, None)
    status, result = without_recovery.route("POST", path, f"Bearer {pending.token}", {"choice": "fresh"})
    assert status == 404


def test_unconfigured_controller_recovery_answers_unavailable(pending):
    pending.service.recover = None
    status, result = post(pending, pending.token, {"agent": "coordinator", "choice": "fresh"})
    assert (status, result["error_class"], result["message"]) == (
        503,
        "dependency_unavailable",
        "controller recovery is unavailable",
    )


def test_missing_caller_name_never_uses_a_display_label_default(pending):
    slug = pending.recovery.slug
    name = "XXXX"
    pending.world.store.put_agent(slug, AgentRecord(name, "master", ""))
    token = auth.issue_agent(pending.world.store.redis, "fixture", slug, name)
    status, result = post(pending, token, {"choice": "fresh"})
    assert (status, result["error_class"]) == (401, "unauthenticated")
    assert pending.recovery.decision(pending.old.execution_id).mode == "recovery_pending"


def test_restore_transport_uses_json_a_canonical_url_and_a_bounded_deadline(pending, monkeypatch):
    from scripts.gates import Who
    from scripts.swarm_v2 import control_service

    slug = pending.recovery.slug
    base = pending.url.split("/v1/")[0]
    pending.world.store.update(slug, api_url=base + "/")
    monkeypatch.setattr(
        control_service,
        "page_credential",
        lambda requested: "fixture operator credential" if requested == slug else None,
    )
    opening = urllib.request.urlopen
    sent = []

    def checked(request, timeout):
        sent.append(request.full_url)
        assert request.full_url == pending.url
        assert request.get_method() == "POST"
        assert request.get_header("Content-type") == "application/json"
        assert timeout == 10
        return opening(request, timeout=timeout)

    monkeypatch.setattr(urllib.request, "urlopen", checked)
    result = decide(pending.world.store, slug, pending.old, "fresh", Who())
    assert result["mode"] == "fresh"
    assert sent == [pending.url]
    pending.world.store.update(slug, api_url="https://controller.example/X")
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout: pytest.fail(request.full_url))
    with pytest.raises(pytest.fail.Exception, match="controller.example/X/v1/"):
        decide(pending.world.store, slug, pending.old, "fresh", Who())


def test_a_malformed_caller_is_rejected_before_the_credential_store(pending, monkeypatch):
    from redis.exceptions import RedisError

    def unavailable(*args):
        raise RedisError("synthetic outage")

    monkeypatch.setattr(auth, "agent_member", unavailable)
    status, result = post(pending, pending.token, {"agent": 5, "choice": "fresh"})
    assert (status, result["error_class"]) == (401, "unauthenticated")
