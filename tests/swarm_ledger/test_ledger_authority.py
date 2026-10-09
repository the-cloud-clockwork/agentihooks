import http.client
import json
import os
import sys
import threading
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))
import ledger as cli_ledger  # noqa: E402
import ledger_core as core  # noqa: E402
import new_ledger  # noqa: E402

from scripts.swarm.ledger_client import LedgerClient  # noqa: E402
from scripts.swarm.store import SwarmError  # noqa: E402
from scripts.swarm_ledger import ledger, ledger_server  # noqa: E402
from scripts.swarm_ledger import ledger_authority as authority  # noqa: E402

server = ledger_server
from tests.swarm_ledger.test_media import png  # noqa: E402

SLUG = "authority-proof"
WORKER = "engineer@323133-0256"
OTHER = "engineer@323133-0257"
MASTER = "master@323133-0001"


pytestmark = pytest.mark.xdist_group("fakeredis")


def operation(kind, **fields):
    return {"op": kind, "id": uuid.uuid4().hex, **fields}


def pinned(name=WORKER):
    return patch.dict(os.environ, {"AGENTIHOOKS_SWARM": "rig-grade-swarm", "AGENTIHOOKS_AGENT_NAME": name})


@pytest.fixture(scope="module")
def live():
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = httpd.server_address[1]
    host = f"127.0.0.1:{port}"
    content = {"title": "Authority", "phases": [{"title": "Proof", "description": "word " * 101}]}
    page = new_ledger.render(new_ledger.build_doc(content), SLUG, port)
    core.paths(SLUG)[0].write_text(page)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    with (
        patch("hooks._redis.get_redis", return_value=None),
        patch("scripts.gates.talk.Budget._marks", return_value=None),
        patch.object(server, "relay_to_inbox", return_value=None),
        patch.object(server, "doctor_phrase", return_value=None),
        patch.object(server, "ALLOWED_HOSTS", {host}),
        patch.object(ledger, "BASE", f"http://{host}"),
        patch.object(cli_ledger, "BASE", f"http://{host}"),
    ):
        thread.start()
        try:
            yield {"port": port, "host": host, "admin": core.read_token(page)}
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)


def send(live, method, path, body=b"", **headers):
    conn = http.client.HTTPConnection("127.0.0.1", live["port"], timeout=5)
    try:
        conn.request(method, path, body, {"Host": live["host"], **headers})
        response = conn.getresponse()
        return response.status, response.read(), response.getheader("Content-Type")
    finally:
        conn.close()


def admin_put(live, *ops):
    headers = {"Content-Type": "application/json", "X-Ledger-Token": live["admin"]}
    status, data, _ = send(live, "PUT", f"/api/{SLUG}?view=agent", json.dumps({"ops": list(ops)}), **headers)
    return status, json.loads(data) if status == 200 else data


def agent_headers(live, name, header=None):
    return {"X-Ledger-Token": authority.agent_token(live["admin"], SLUG, name), "X-Ledger-Agent": header or name}


@pytest.fixture(scope="module")
def crew(live):
    assert admin_put(live, operation("join", by=WORKER), operation("join", by=OTHER))[0] == 200
    assert admin_put(live, operation("join", by=MASTER, role="orchestrator"))[0] == 200
    return live


def tasks(reply):
    return {task["id"] for task in reply["tasks"]}


def test_pinned_worker_transport_cannot_create_a_task_as_master(crew):
    with pinned():
        own = operation("add", by=WORKER, thread="chat", to="operator", text="Worker control")
        assert own["id"] not in ledger.request(SLUG, [own])["rejected"]
        denied = operation("task_add", by=WORKER, task="t1", title="Own author", lane="eng", phase="p1")
        assert denied["id"] in ledger.request(SLUG, [denied])["rejected"]
        forged = operation("task_add", by=MASTER, task="t1", title="Forged author", lane="eng", phase="p1")
        reply = ledger.request(SLUG, [forged])
    assert forged["id"] in reply["rejected"]
    assert "t1" not in tasks(ledger.request(SLUG))
    assert reply["_meta"]["warnings"] == [
        "phase p1 description has 101 words, limit 100",
        f"{WORKER} cannot write as {MASTER}",
    ]


def test_pinned_worker_cannot_write_as_another_worker_or_the_operator(crew):
    other = operation("add", by=OTHER, thread="chat", to="operator", text="Other worker")
    unsigned = operation("add", thread="chat", text="Operator words")
    with pinned():
        reply = ledger.request(SLUG, [other, unsigned])
    assert set(reply["rejected"]) >= {other["id"], unsigned["id"]}
    texts = [entry["text"] for entry in ledger.resource(SLUG, "chat", collection=True)]
    assert "Other worker" not in texts
    assert "Operator words" not in texts
    assert "only the operator writes without an author" in reply["_meta"]["warnings"]


def test_a_worker_cannot_raise_its_own_role_but_a_master_can_join_as_orchestrator(crew):
    with pinned():
        raised = operation("join", by=WORKER, role="orchestrator")
        reply = ledger.request(SLUG, [raised])
    assert raised["id"] in reply["rejected"]
    assert ledger.request(SLUG)["_meta"]["members"][WORKER]["role"] == "member"
    assert f"{WORKER} cannot join as orchestrator" in reply["_meta"]["warnings"]
    with pinned(MASTER):
        joined = operation("join", by=MASTER, role="orchestrator")
        reply = ledger.request(SLUG, [joined])
    assert joined["id"] not in reply["rejected"]
    assert ledger.request(SLUG)["_meta"]["members"][MASTER]["role"] == "orchestrator"


def test_a_bound_master_still_adds_tasks(crew):
    with pinned(MASTER):
        added = operation("task_add", by=MASTER, task="t9", title="Master task", lane="eng", phase="p1")
        reply = ledger.request(SLUG, [added])
    assert added["id"] not in reply["rejected"]
    assert "t9" in tasks(ledger.request(SLUG))


def test_an_alias_of_the_bound_name_writes_as_that_agent(crew):
    alias = "engineer-323133-0256"
    with pinned(), patch.object(server.authority, "resolve_name", lambda name: WORKER if name == alias else name):
        said = operation("add", by=alias, thread="chat", to="operator", text="Alias control")
        reply = ledger.request(SLUG, [said])
    assert said["id"] not in reply["rejected"]


def test_the_operator_credential_keeps_full_administration(crew):
    status, reply = admin_put(
        crew,
        operation("add", thread="chat", text="Operator control"),
        operation("join", by=OTHER, role="orchestrator"),
    )
    assert status == 200
    assert reply["rejected"] == []
    assert ledger.request(SLUG)["_meta"]["members"][OTHER]["role"] == "orchestrator"
    assert admin_put(crew, operation("join", by=OTHER, role="member"))[0] == 200
    body = json.dumps({"changes": [{"path": "phases/p1/done", "value": True}]})
    headers = {"Content-Type": "application/json", "X-Ledger-Token": crew["admin"]}
    status, data, _ = send(crew, "PUT", f"/api/{SLUG}?view=agent", body, **headers)
    assert status == 200
    assert json.loads(data)["phases"][0]["done"] is True


def test_the_swarm_client_keeps_service_authority_inside_a_pinned_session(crew):
    with pinned():
        LedgerClient().say(SLUG, "Service control", by="swarm")
        LedgerClient().say(SLUG, "Unsigned control")
        LedgerClient(service=True).say(SLUG, "Tick relay", by=OTHER)
        chat = LedgerClient().chat(SLUG)
    said = [(entry.get("by"), entry["text"]) for entry in chat]
    assert ("swarm", "Service control") in said
    assert ("operator", "Unsigned control") in said
    assert (OTHER, "Tick relay") in said


def test_the_swarm_client_binds_agent_authored_writes_to_the_session(crew):
    with pinned():
        LedgerClient().say(SLUG, "Own swarm line", by=WORKER)
        with pytest.raises(SwarmError) as refused:
            LedgerClient().say(SLUG, "Forged swarm line", by=MASTER)
        chat = LedgerClient().chat(SLUG)
    said = [(entry.get("by"), entry["text"]) for entry in chat]
    assert (WORKER, "Own swarm line") in said
    assert "Forged swarm line" not in [text for _, text in said]
    assert str(refused.value).endswith(
        f"refused: phase p1 description has 101 words, limit 100; {WORKER} cannot write as {MASTER}"
    )


def test_a_credential_for_one_name_refuses_another_agent_header(crew):
    body = json.dumps({"ops": [operation("add", by=OTHER, thread="chat", to="operator", text="Header swap")]})
    headers = {"Content-Type": "application/json", **agent_headers(crew, WORKER, header=OTHER)}
    before = core.paths(SLUG)[1].read_bytes()
    refused = send(crew, "PUT", f"/api/{SLUG}?view=agent", body, **headers)
    assert refused == (403, b"missing or wrong ledger token", "text/plain")
    assert core.paths(SLUG)[1].read_bytes() == before


def test_a_worker_cannot_send_page_changes(crew):
    body = json.dumps({"changes": [{"path": "title", "value": "Taken"}]})
    headers = {"Content-Type": "application/json", **agent_headers(crew, WORKER)}
    before = core.paths(SLUG)[1].read_bytes()
    refused = send(crew, "PUT", f"/api/{SLUG}?view=agent", body, **headers)
    assert refused == (403, b"page changes need the operator", "text/plain")
    assert core.paths(SLUG)[1].read_bytes() == before


def test_administrative_swarm_controls_need_the_operator(crew):
    body = json.dumps({"action": "start"})
    with patch.object(server, "swarm_control", return_value=({"state": "running"}, "")) as control:
        worker = send(crew, "PUT", f"/api/swarm/{SLUG}", body, **agent_headers(crew, WORKER))
        assert worker == (403, b"swarm controls need the operator", "text/plain")
        control.assert_not_called()
        status, _, _ = send(crew, "PUT", f"/api/swarm/{SLUG}", body, **{"X-Ledger-Token": crew["admin"]})
    assert status == 200
    control.assert_called_once()


def test_an_upload_is_bound_to_the_credential_name(crew):
    image = png()
    own = send(crew, "POST", f"/api/media/{SLUG}", image, **agent_headers(crew, WORKER))
    assert own[0] == 200
    forged = send(crew, "POST", f"/api/media/{SLUG}", image, **agent_headers(crew, WORKER, header=MASTER))
    assert forged == (403, b"missing or wrong ledger token", "text/plain")
    with pinned():
        upload = core.LEDGER_DIR / "upload.png"
        upload.write_bytes(image)
        with pytest.raises(SystemExit) as refused:
            ledger.upload_image(SLUG, MASTER, str(upload))
        assert ledger.upload_image(SLUG, WORKER, str(upload))["type"] == "image/png"
    error = json.loads(str(refused.value).split("403 ", 1)[1])
    assert error["error"]["code"] == "forbidden"


def test_the_principal_resolves_from_the_credential():
    token = authority.agent_token("admin-secret", SLUG, WORKER)
    assert authority.principal("admin-secret", SLUG, "admin-secret", None) == ""
    assert authority.principal("admin-secret", SLUG, "admin-secret", WORKER) == ""
    assert authority.principal("admin-secret", SLUG, token, WORKER) == WORKER
    assert authority.principal("admin-secret", SLUG, token, OTHER) is None
    assert authority.principal("admin-secret", "other-ledger", token, WORKER) is None
    assert authority.principal("admin-secret", SLUG, token, None) is None
    assert authority.principal("admin-secret", SLUG, "", None) is None
    assert authority.principal(None, SLUG, "", None) is None
    assert authority.principal("", SLUG, "", None) is None
    assert authority.principal("", SLUG, authority.agent_token("", SLUG, WORKER), WORKER) is None
    assert authority.principal("admin-secret", SLUG, "admin-secrét", None) is None


def test_call_keeps_the_service_choice_when_it_retries_after_starting_the_server():
    seen = []

    def request(slug, ops, service):
        seen.append((slug, ops, service))
        if len(seen) % 2:
            raise OSError("not answering")
        return {"rejected": []}

    with patch.object(ledger, "request", request), patch.object(ledger.subprocess, "run") as run:
        with patch.object(ledger.repository, "exists", lambda slug: True), patch.dict(os.environ, LEDGER_AUTOSTART="1"):
            assert ledger.call(SLUG, [], service=True) == {"rejected": []}
            assert ledger.call(SLUG) == {"rejected": []}
    assert seen == [(SLUG, [], True), (SLUG, [], True), (SLUG, None, False), (SLUG, None, False)]
    assert run.call_count == 2


def test_call_prints_the_refusal_when_the_retry_after_starting_the_server_is_refused():
    import io
    import urllib.error

    body = b'{"error": {"code": "schema_invalid", "message": "chat refused: clock time \'15:45 UTC\'"}}'
    replies = [OSError("not answering"), urllib.error.HTTPError(ledger.BASE, 400, "Bad Request", {}, io.BytesIO(body))]

    def request(slug, ops, service):
        raise replies.pop(0)

    with patch.object(ledger, "request", request), patch.object(ledger.subprocess, "run"):
        with patch.object(ledger.repository, "exists", lambda slug: True), pytest.raises(SystemExit) as exit_:
            ledger.call(SLUG, [])
    assert str(exit_.value) == f"server refused: 400 {body.decode()}"


def test_a_page_without_a_token_sends_an_empty_credential(live):
    core.paths("tokenless")[0].write_text("<html></html>")
    with patch.dict(os.environ, {"AGENTIHOOKS_SWARM": ""}):
        assert ledger.credentials("tokenless") == {"X-Ledger-Token": ""}


def test_refusals_name_why_each_op_is_refused():
    with patch.object(authority, "resolve_name", lambda name: name):
        assert authority.refusal(WORKER, {"by": WORKER, "op": "add"}) == ""
        assert authority.refusal(WORKER, {"op": "add"}) == "only the operator writes without an author"
        assert authority.refusal(WORKER, {"by": MASTER, "op": "add"}) == f"{WORKER} cannot write as {MASTER}"
        assert authority.refusal(WORKER, {"by": WORKER, "op": "join"}) == ""
        assert authority.refusal(WORKER, {"by": WORKER, "op": "join", "role": "member"}) == ""
        assert (
            authority.refusal(WORKER, {"by": WORKER, "op": "join", "role": "orchestrator"})
            == f"{WORKER} cannot join as orchestrator"
        )
        assert authority.refusal(MASTER, {"by": MASTER, "op": "join", "role": "orchestrator"}) == ""
        assert authority.refusal("rig-master-1", {"by": "rig-master-1", "op": "join", "role": "orchestrator"}) == ""
        assert authority.refusal("", {"op": "add"}) == ""


def test_the_transport_selects_the_bound_credential_only_in_a_pinned_session(crew):
    with patch.dict(os.environ, {"AGENTIHOOKS_SWARM": "", "AGENTIHOOKS_AGENT_NAME": WORKER}):
        assert ledger.credentials(SLUG) == {"X-Ledger-Token": crew["admin"]}
    with pinned():
        assert ledger.credentials(SLUG) == agent_headers(crew, WORKER)
        assert ledger.credentials(SLUG, service=True) == {"X-Ledger-Token": crew["admin"]}


def test_controller_ledger_operations_carry_epoch(monkeypatch):
    from types import SimpleNamespace

    import fakeredis

    from scripts.swarm import controller, lease, ledger_client
    from scripts.swarm.store import RedisStore

    saved = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    held = lease.acquire(saved, SLUG, "home")
    calls = []
    transport = SimpleNamespace(call=lambda slug, ops, service: calls.append(ops) or {})
    monkeypatch.setattr(ledger_client, "_ledger", lambda: transport)
    client = LedgerClient()
    controller.FencedLedger(saved, SLUG, held, client).say(SLUG, "Fenced write", by="swarm")
    assert calls[0][0]["controller_epoch"] == 1
    client.say(SLUG, "Ordinary write", by="swarm")
    assert "controller_epoch" not in calls[1][0]


def test_takeover_before_repository_write_refuses_stale_controller(crew, monkeypatch):
    import fakeredis

    from scripts.swarm import controller, lease
    from scripts.swarm.ledger_client import LedgerRefused
    from scripts.swarm.store import RedisStore

    saved = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    held = lease.acquire(saved, SLUG, "home")
    monkeypatch.setattr(server.authority, "connect", lambda: saved)
    apply = server.repository.apply_ops

    def takeover(*args, **kwargs):
        assert lease.release(saved, SLUG, held)
        lease.acquire(saved, SLUG, "other")
        return apply(*args, **kwargs)

    monkeypatch.setattr(server.repository, "apply_ops", takeover)
    with pytest.raises(LedgerRefused) as error:
        controller.FencedLedger(saved, SLUG, held, LedgerClient()).say(SLUG, "Stale controller write", by="swarm")
    assert "the controller lease is stale" in str(error.value)
    assert all(row["text"] != "Stale controller write" for row in LedgerClient().chat(SLUG))


def test_epoch_authority_accepts_current_and_refuses_stale(monkeypatch):
    import fakeredis

    from scripts.swarm import lease
    from scripts.swarm.store import RedisStore

    saved = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    held = lease.acquire(saved, SLUG, "home")
    monkeypatch.setattr(authority, "connect", lambda: saved)
    assert authority.fence(SLUG, held.epoch) is None
    with pytest.raises(SwarmError) as error:
        authority.fence(SLUG, held.epoch + 1)
    assert str(error.value) == "the controller lease is stale"


@pytest.mark.parametrize("epoch", [0, -1, None, "1", 1.5, True])
def test_controller_epoch_schema_rejects_invalid_epochs(epoch):
    from scripts.swarm_ledger.api import schemas
    from scripts.swarm_ledger.api.errors import APIError

    op = {"op": "add", "id": "one", "thread": "chat", "text": "Write", "controller_epoch": epoch}
    with pytest.raises(APIError) as error:
        schemas.validate(schemas.operation_schema("add"), op)
    assert error.value.status == 400
    assert error.value.code == "schema_invalid"


def test_controller_epoch_schema_accepts_one_and_binds_operation_kind():
    from scripts.swarm_ledger.api import schemas
    from scripts.swarm_ledger.api.errors import APIError

    op = {"op": "add", "id": "one", "thread": "chat", "text": "Write", "controller_epoch": 1}
    assert schemas.validate(schemas.operation_schema("add"), op) is None
    op["op"] = "clear"
    with pytest.raises(APIError) as error:
        schemas.validate(schemas.operation_schema("add"), op)
    assert error.value.status == 400


def test_domain_validation_receives_operations_without_controller_metadata():
    from types import SimpleNamespace

    from scripts.swarm_ledger.api import schemas

    seen = []
    op = {"op": "add", "id": "one", "thread": "chat", "text": "Write", "controller_epoch": 1}
    payload = {"operation_id": "one", "ops": [op], "guards": {"chat": "0" * 64}}
    core = SimpleNamespace(check_body=lambda body, task_ids: seen.append((body, task_ids)))
    assert schemas.check_operations(payload, core, ()) == [op]
    assert seen == [({"ops": [{"op": "add", "id": "one", "thread": "chat", "text": "Write"}]}, ())]


def _epoch_mutation_server(rejected):
    from types import SimpleNamespace

    from scripts.swarm_ledger.api import resources

    doc = {"chat": [], "tasks": [], "_meta": {"rev": 1, "warnings": []}}
    ctx = SimpleNamespace(meta={}, dirty=False)
    seen = []

    def fence(slug, epoch):
        assert slug == "sw"
        assert epoch == 1
        seen.append((slug, epoch))
        if rejected:
            raise SwarmError("the controller lease is stale")

    def apply_ops(slug, ops, gate):
        assert slug == "sw"
        refused = [op["id"] for op in ops if not gate.apply(doc, op, ctx, None)]
        return doc, refused

    server = SimpleNamespace(
        repository=SimpleNamespace(get_document=lambda *args, **kwargs: doc, apply_ops=apply_ops),
        authority=SimpleNamespace(refusal=lambda *args: "", fence=fence),
        core=SimpleNamespace(check_body=lambda *args: None),
        talk=SimpleNamespace(Budget=lambda slug: SimpleNamespace(apply=lambda *args: True)),
        ledger_media=SimpleNamespace(resolve=lambda *args: None),
        ledger_artifacts=SimpleNamespace(resolve=lambda *args: None),
        relay_to_inbox=lambda *args: None,
        doctor_phrase=lambda *args: None,
    )
    op = {"op": "add", "id": "one", "by": "swarm", "thread": "chat", "text": "Write", "controller_epoch": 1}
    payload = {"operation_id": "one", "ops": [op], "guards": {"chat": resources.resource_revision(doc, "chat")}}
    return server, payload, seen


def test_mutation_receiver_checks_epoch_before_and_after_domain_apply():
    from scripts.swarm_ledger.api import mutations

    server, payload, seen = _epoch_mutation_server(False)
    reply = mutations.apply(server, "sw", "", payload)
    assert seen == [("sw", 1), ("sw", 1)]
    assert reply == {"applied": ["one"], "rejected": [], "_meta": {"rev": 1, "warnings": []}}


def test_mutation_receiver_returns_exact_stale_epoch_error():
    from scripts.swarm_ledger.api import mutations
    from scripts.swarm_ledger.api.errors import APIError

    server, payload, seen = _epoch_mutation_server(True)
    with pytest.raises(APIError) as error:
        mutations.apply(server, "sw", "", payload)
    assert seen == [("sw", 1)]
    assert error.value.status == 409
    assert error.value.code == "stale_controller"
    assert error.value.envelope() == {"error": {"code": "stale_controller", "message": "the controller lease is stale"}}
