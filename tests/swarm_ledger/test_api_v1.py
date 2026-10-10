import json
import uuid

from tests.swarm_ledger.test_ledger_authority import SLUG, send, server

RELAY = server.relay_to_inbox


import pytest

pytestmark = pytest.mark.xdist_group("fakeredis")


from tests.swarm_ledger.test_ledger_authority import live as _authority_live

authority_live = _authority_live


_talk_marks = server.talk.Budget._marks
_talk_init = server.talk.Budget.__init__


@pytest.fixture
def live(authority_live):
    from tests.swarm_ledger import legacy_page
    from tests.swarm_ledger.test_ledger_authority import core, new_ledger

    content = {"title": "Authority", "phases": [{"title": "Proof", "description": "word " * 101}]}
    page = legacy_page.render(new_ledger.build_doc(content), SLUG, authority_live["port"])
    html, document = core.paths(SLUG)
    html.write_text(page)
    document.unlink(missing_ok=True)
    core.sync(
        SLUG,
        ops=[
            {"op": "join", "id": "seed-join", "by": "api-reader"},
            {"op": "add", "id": "seed-chat-one", "thread": "chat", "text": "First message"},
            {"op": "add", "id": "seed-chat-two", "thread": "chat", "text": "Second message"},
        ],
    )
    return {**authority_live, "admin": core.read_token(page)}


from scripts.swarm_ledger import api as api
from scripts.swarm_ledger import ledger as ledger
from scripts.swarm_ledger import ledger_server as ledger_server
from scripts.swarm_ledger.api import admin as admin
from scripts.swarm_ledger.api import errors as errors
from scripts.swarm_ledger.api import mutations as mutations
from scripts.swarm_ledger.api import routes as routes


def request(live, method, resource, body=None, **headers):
    if resource == "operations" and isinstance(body, dict):
        body.setdefault("operation_id", uuid.uuid4().hex)
    status, data, _ = send(
        live,
        method,
        f"/api/v1/ledgers/{SLUG}/{resource}",
        b"" if body is None else json.dumps(body).encode(),
        **{"X-Ledger-Token": live["admin"], "Content-Type": "application/json", **headers},
    )
    return status, json.loads(data)


def test_invalid_schema_leaves_resource_unchanged(live):
    status, before = request(live, "GET", "metadata")
    assert status == 200
    status, error = request(live, "POST", "operations", {"ops": "invalid"})
    assert status == 400
    assert error["error"]["code"] == "schema_invalid"
    assert request(live, "GET", "metadata") == (200, before)


def test_paginated_threads_and_independent_resource_revisions(live):
    status, phases = request(live, "GET", "phases?limit=1")
    assert status == 200
    assert len(phases["data"]) == 1
    phase = phases["data"][0]
    assert "comments" not in phase
    before = request(live, "GET", f"phases/{phase['id']}")
    status, _, _ = send(
        live,
        "PUT",
        f"/api/{SLUG}",
        json.dumps(
            {
                "ops": [
                    {"op": "add", "id": "page-chat-one", "thread": "chat", "text": "First message"},
                    {"op": "add", "id": "page-chat-two", "thread": "chat", "text": "Second message"},
                ]
            }
        ).encode(),
        **{"X-Ledger-Token": live["admin"], "Content-Type": "application/json"},
    )
    assert status == 200
    status, first = request(live, "GET", "chat?limit=1")
    assert status == 200
    assert len(first["data"]) == 1
    assert first["next_cursor"] is not None
    status, second = request(live, "GET", f"chat?limit=1&cursor={first['next_cursor']}")
    assert status == 200
    assert second["data"][0]["id"] != first["data"][0]["id"]
    assert request(live, "GET", f"phases/{phase['id']}") == before
    assert request(live, "GET", "chat?limit=101")[0] == 400


def test_forbidden_caller_cannot_mutate(live):
    before = request(live, "GET", "metadata")
    status, reply = request(
        live,
        "POST",
        "operations",
        {
            "ops": [{"op": "add", "id": "forbidden-message", "thread": "chat", "text": "Refused"}],
            "guards": {"metadata": before[1]["revision"]},
        },
        **{"X-Ledger-Token": "invalid"},
    )
    assert status == 403
    assert reply["error"]["code"] == "forbidden"
    assert request(live, "GET", "metadata") == before


def test_retry_and_conflict_preserve_one_domain_write(live):
    status, before = request(live, "GET", "chat")
    assert status == 200
    operation = {"op": "add", "id": "retry-message", "thread": "chat", "text": "Once"}
    payload = {"ops": [operation], "guards": {"chat": before["revision"]}}
    status, ack = request(live, "POST", "operations", payload)
    assert status == 200
    assert ack["applied"] == ["retry-message"]
    assert "chat" not in ack
    assert request(live, "POST", "operations", payload)[0] == 200
    status, conflict = request(
        live,
        "POST",
        "operations",
        {
            **payload,
            "operation_id": uuid.uuid4().hex,
            "ops": [{**operation, "id": "stale-message"}],
        },
    )
    assert status == 409
    assert conflict["error"]["code"] == "revision_conflict"
    status, after = request(live, "GET", "chat")
    assert status == 200
    assert len([row for row in after["data"] if row["id"] == "retry-message"]) == 1
    assert not any(row["id"] == "stale-message" for row in after["data"])


def test_cli_and_tick_read_resources_and_return_bounded_write_ack(live):
    from types import SimpleNamespace
    from unittest.mock import patch

    from scripts.swarm.ledger_client import LedgerClient
    from tests.swarm_ledger.test_ledger_authority import ledger

    with patch.dict("os.environ", {"AGENTIHOOKS_AGENT_NAME": "", "AGENTIHOOKS_SWARM": ""}):
        state = ledger.request(SLUG, [{"op": "join", "id": "cli-join", "by": "api-reader"}])
        assert "chat" not in state
        assert state["rejected"] == []
        ledger.cmd_status(SimpleNamespace(slug=SLUG, name="api-reader"))
        client = LedgerClient(service=True)
        assert client.chat(SLUG)
        assert client.tasks(SLUG) == []
        assert client.events(SLUG)
        assert client.state(SLUG)["phases"]
        assert not client.closed(SLUG)


def test_operation_contract_covers_existing_domain_dispatch():
    from scripts.swarm_ledger.api import schemas
    from tests.swarm_ledger.test_ledger_authority import core

    kinds = {
        "add",
        "edit",
        "delete",
        "clear",
        *core.SYNC_KINDS,
        *core.EXTENSION_OPS,
        *core.AGENT_OPS,
        *core.ARTIFACT_OPS,
    }
    assert set(schemas.FIELDS) == kinds
    assert schemas.FIELDS["title_set"] == "text"


def test_export_is_explicit_and_summary_is_paginated(live):
    status, exported = request(live, "POST", "export", {})
    assert status == 200
    assert "phases" in exported["data"]
    assert "seeds" not in exported["data"]["_meta"]
    status, data, _ = send(live, "GET", "/api/v1/ledgers?limit=1")
    assert status == 200
    summary = json.loads(data)
    assert len(summary["data"]) <= 1
    assert "revision" in summary


def test_all_collection_readers_and_swarm_control_reader(live):
    from unittest.mock import patch

    from scripts.swarm_ledger.api.resources import COLLECTIONS
    from tests.swarm_ledger.test_ledger_authority import server

    for resource in (*COLLECTIONS, "members", "events"):
        status, reply = request(live, "GET", f"{resource}?limit=1")
        assert status == 200
        assert len(reply["data"]) <= 1
    swarm = {
        "state": "paused",
        "agents": [{"name": "first"}, {"name": "second"}],
        "health": [],
        "config": {"autonomy": "delegate"},
    }
    with patch.object(server, "swarm_status", return_value=swarm):
        status, reader = request(live, "GET", "swarm")
        assert status == 200
        assert "agents" not in reader["data"]
        status, agents = request(live, "GET", "swarm/agents?limit=1")
        assert status == 200
        assert len(agents["data"]) == 1


def test_worker_authority_and_operation_identifier_collision(live):
    from tests.swarm_ledger.test_ledger_authority import authority

    headers = {
        "X-Ledger-Agent": "api-reader",
        "X-Ledger-Token": authority.agent_token(live["admin"], SLUG, "api-reader"),
    }
    rev = request(live, "GET", "members")[1]["revision"]
    status, denied = request(
        live,
        "POST",
        "operations",
        {
            "ops": [{"op": "join", "id": "raised-role", "by": "api-reader", "role": "orchestrator"}],
            "guards": {"members": rev},
        },
        **headers,
    )
    assert status == 403
    assert denied["error"]["code"] == "forbidden"
    rev = request(live, "GET", "chat")[1]["revision"]
    original = {"op": "add", "id": "retry-message", "thread": "chat", "text": "Once"}
    assert (
        request(
            live, "POST", "operations", {"operation_id": "collision-proof", "ops": [original], "guards": {"chat": rev}}
        )[0]
        == 200
    )
    rev = request(live, "GET", "chat")[1]["revision"]
    status, denied = request(
        live,
        "POST",
        "operations",
        {
            "operation_id": "collision-proof",
            "ops": [{**original, "text": "Different content"}],
            "guards": {"chat": rev},
        },
    )
    assert status == 409
    assert denied == {
        "error": {
            "code": "operation_conflict",
            "message": "Operation identifier was already used for different content",
        }
    }


def test_operator_write_reaches_inbox_once(live, monkeypatch):
    import fakeredis

    from scripts.inbox.store import InboxStore
    from scripts.swarm.store import RedisStore, SwarmConfig
    from tests.swarm_ledger.test_ledger_authority import server

    box = InboxStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
    RedisStore(box.redis).create(SwarmConfig(SLUG, "/repo", 1, 0))
    monkeypatch.setattr("scripts.inbox.store.connect", lambda environ=None: box)
    monkeypatch.setattr(server, "relay_to_inbox", RELAY)
    revision = request(live, "GET", "chat")[1]["revision"]
    payload = {
        "ops": [{"op": "add", "id": "inbox-canary", "thread": "chat", "text": "Operator resource canary"}],
        "guards": {"chat": revision},
    }
    assert request(live, "POST", "operations", payload)[0] == 200
    assert request(live, "POST", "operations", payload)[0] == 200
    items = box.pending_items(f"master@{SLUG}")
    assert len(items) == 1
    assert "Operator resource canary" in items[0].text


def test_versioned_upload_validates_headers_and_binds_the_uploader(live):
    from tests.swarm_ledger.test_ledger_authority import authority, core

    core.sync(
        SLUG,
        ops=[
            {
                "op": "task_add",
                "id": "upload-task",
                "by": "swarm",
                "task": "t1",
                "title": "Proof",
                "lane": "eng",
                "artifact": True,
            },
            {
                "op": "task_update",
                "id": "upload-claim",
                "by": "swarm",
                "item": "tasks/t1",
                "fields": {"state": "claimed", "claimed_by": "api-reader"},
            },
        ],
    )
    headers = {
        "X-Artifact-Request": json.dumps({"task": "t1", "title": "Proof"}),
        "X-Ledger-Agent": "api-reader",
        "X-Ledger-Token": authority.agent_token(live["admin"], SLUG, "api-reader"),
        "Content-Type": "application/octet-stream",
        "X-Artifact-Name": "proof.md",
    }
    status, data, _ = send(live, "POST", f"/api/v1/ledgers/{SLUG}/uploads/artifacts", b"# Resource proof", **headers)
    assert status == 200
    assert json.loads(data)["type"] == "text/markdown"
    status, data, _ = send(
        live,
        "POST",
        f"/api/v1/ledgers/{SLUG}/uploads/artifacts",
        b"# Refused",
        **{**headers, "X-Ledger-Agent": "other"},
    )
    assert status == 403
    assert json.loads(data)["error"]["code"] == "forbidden"


def test_versioned_administration_keeps_origin_and_role_checks(live):
    from unittest.mock import patch

    from tests.swarm_ledger.test_ledger_authority import authority, server

    status, data, _ = send(live, "GET", "/api/v1/layout")
    assert status == 200
    assert "revision" in json.loads(data)
    status, data, _ = send(live, "PUT", "/api/v1/layout", b"{}", **{"Content-Type": "application/json"})
    assert status == 403
    assert json.loads(data)["error"]["code"] == "forbidden"
    headers = {
        "X-Ledger-Agent": "api-reader",
        "X-Ledger-Token": authority.agent_token(live["admin"], SLUG, "api-reader"),
    }
    with patch.object(server, "swarm_control") as control:
        assert request(live, "POST", "swarm/actions", {"action": "start"}, **headers) == (
            403,
            {"error": {"code": "forbidden", "message": "Swarm controls need the operator"}},
        )
        control.assert_not_called()
        status, error = request(live, "POST", "swarm/actions", {"action": "start", "surprise": True})
        assert status == 400
        assert error["error"]["code"] == "schema_invalid"
        control.assert_not_called()


def test_operator_checkbox_changes_have_schema_and_guard(live):
    phase = request(live, "GET", "phases")[1]["data"][0]
    path = f"phases/{phase['id']}"
    revision = request(live, "GET", path)[1]["revision"]
    payload = {
        "id": "checkbox-change",
        "ops": [],
        "changes": [{"path": f"{path}/done", "base": False, "value": True}],
        "guards": {path: revision},
    }
    assert (
        request(live, "POST", "operations", {**payload, "changes": [{"path": f"{path}/done", "value": "false"}]})[0]
        == 400
    )
    assert request(live, "GET", path)[1]["revision"] == revision
    first = request(live, "POST", "operations", payload)
    assert first[0] == 200
    assert first[1]["applied"] == ["checkbox-change"]
    assert first[1]["rejected"] == []
    assert request(live, "GET", path)[1]["data"]["done"] is True
    assert request(live, "POST", "operations", payload) == first
    refused = {
        "id": "checkbox-refused",
        "ops": [],
        "changes": [{"path": f"{path}/done", "base": False, "value": False}],
        "guards": {path: request(live, "GET", path)[1]["revision"]},
    }
    status, result = request(live, "POST", "operations", refused)
    assert status == 200
    assert result["applied"] == []
    assert result["rejected"] == ["checkbox-refused"]
    assert request(live, "GET", path)[1]["data"]["done"] is True


def test_each_domain_schema_rejects_unknown_fields_without_mutation(live):
    from scripts.swarm_ledger.api.schemas import FIELDS

    before = request(live, "GET", "metadata")
    for kind in FIELDS:
        status, reply = request(
            live,
            "POST",
            "operations",
            {
                "ops": [{"op": kind, "id": f"invalid-{kind}", "unexpected": True}],
                "guards": {},
            },
        )
        assert status == 400
        message = f"Operation {kind} does not match its schema at field unexpected"
        assert reply == {"error": {"code": "schema_invalid", "message": message}}
    assert request(live, "GET", "metadata") == before


def test_a_schema_mismatch_names_the_first_unknown_key_and_the_top_field(live):
    cases = [
        ({"op": "sync", "id": "two-unknown", "zz": 1, "aa": 1}, "sync", "aa"),
        ({"op": "task_add", "id": "nested", "depends_on": [1]}, "task_add", "depends_on"),
    ]
    for operation, kind, field in cases:
        message = f"Operation {kind} does not match its schema at field {field}"
        assert request(live, "POST", "operations", {"ops": [operation], "guards": {}}) == (
            400,
            {"error": {"code": "schema_invalid", "message": message}},
        )


def test_stale_cursor_missing_guard_and_origin_are_rejected(live):
    status, page = request(live, "GET", "chat?limit=1")
    assert status == 200
    payload = {"ops": [{"op": "add", "id": "cursor-change", "thread": "chat", "text": "Cursor changed"}], "guards": {}}
    status, reply = request(live, "POST", "operations", payload)
    assert status == 428
    assert reply == {
        "error": {"code": "revision_required", "message": "Every changed resource needs an expected revision"}
    }
    assert request(live, "POST", "operations", payload, Origin="https://untrusted.example")[0] == 403
    payload["guards"] = {"chat": page["revision"]}
    assert request(live, "POST", "operations", payload)[0] == 200
    assert request(live, "GET", f"chat?cursor={page['next_cursor']}")[0] == 409
    assert request(live, "GET", "chat?limit=1&limit=2")[0] == 400


def test_named_item_and_thread_resources(live):
    phase = request(live, "GET", "phases")[1]["data"][0]
    path = f"phases/{phase['id']}/comments"
    rev = request(live, "GET", path)[1]["revision"]
    op = {"op": "add", "id": "item-comment", "thread": path, "text": "Resource comment"}
    assert request(live, "POST", "operations", {"ops": [op], "guards": {path: rev}})[0] == 200
    rows = request(live, "GET", f"{path}?limit=1")[1]["data"]
    assert rows[0]["text"] == "Resource comment"
    assert "comments" not in request(live, "GET", f"phases/{phase['id']}")[1]["data"]


def test_guarded_task_update_returns_only_the_requested_row(live):
    from scripts.swarm.ledger_client import LedgerClient
    from tests.swarm_ledger.test_ledger_authority import ledger

    ledger.request(SLUG, [{"op": "join", "id": "planner-join", "by": "boss", "role": "orchestrator"}], service=True)
    ledger.request(
        SLUG,
        [
            {
                "op": "task_add",
                "id": "task-create",
                "by": "boss",
                "task": "t1",
                "title": "Resource proof",
                "lane": "eng",
                "phase": "p1",
            }
        ],
        service=True,
    )
    client = LedgerClient(service=True)
    row = client.update_task(SLUG, "t1", {"state": "claimed", "claimed_by": "api-reader"}, if_state=("open",))
    assert row["id"] == "t1"
    assert row["state"] == "claimed"
    assert "comments" not in row


def test_repeated_edit_and_delete_keep_entry_identity(live):
    from scripts.swarm_ledger.api.client import ResourceClient
    from tests.swarm_ledger.test_ledger_authority import ledger

    client = ResourceClient(ledger.BASE, ledger.credentials(SLUG, service=True))
    added = {"op": "add", "id": "edited-entry", "thread": "chat", "text": "Before"}
    assert client.mutate(SLUG, [added])["rejected"] == []
    first = {"op": "edit", "id": "edited-entry", "thread": "chat", "text": "First edit"}
    assert client.mutate(SLUG, [first])["rejected"] == []
    assert client.mutate(SLUG, [first])["rejected"] == []
    second = {"op": "edit", "id": "edited-entry", "thread": "chat", "text": "Second edit"}
    assert client.mutate(SLUG, [second])["rejected"] == []
    deleted = {"op": "delete", "id": "edited-entry", "thread": "chat"}
    assert client.mutate(SLUG, [deleted])["rejected"] == []
    rows = client.collection(SLUG, "chat")
    row = next(row for row in rows if row["id"] == "edited-entry")
    assert row["deleted"] is True
    assert row["text"] == ""


def test_composite_client_state_keeps_edited_threads(live):
    from scripts.swarm.ledger_client import LedgerClient
    from scripts.swarm_ledger.api.client import ResourceClient
    from tests.swarm_ledger.test_ledger_authority import ledger

    client = ResourceClient(ledger.BASE, ledger.credentials(SLUG, service=True))
    client.mutate(SLUG, [{"op": "add", "id": "edited-comment", "thread": "phases/p1/comments", "text": "Before"}])
    client.mutate(SLUG, [{"op": "edit", "id": "edited-comment", "thread": "phases/p1/comments", "text": "Resolved"}])
    state = LedgerClient(service=True).state(SLUG)
    assert state["phases"][0]["comments"][0]["text"] == "Resolved"


def test_swarm_metadata_respects_total_response_byte_limit(live):
    from unittest.mock import patch

    from scripts.swarm_ledger.api.resources import MAX_REPLY

    with patch.object(server, "swarm_status", return_value={"config": {"description": "x" * MAX_REPLY}}):
        status, reply = request(live, "GET", "swarm")
    assert status == 413
    assert reply == {
        "error": {"code": "resource_too_large", "message": "Use the explicit export operation for this resource"}
    }


def test_large_task_update_returns_a_bounded_acknowledgment(live):
    from scripts.swarm_ledger.api.client import ResourceClient
    from scripts.swarm_ledger.api.resources import MAX_REPLY
    from tests.swarm_ledger.test_ledger_authority import ledger

    client = ResourceClient(ledger.BASE, ledger.credentials(SLUG, service=True))
    client.mutate(
        SLUG, [{"op": "task_add", "id": "big-task", "by": "swarm", "task": "t1", "title": "Proof", "lane": "eng"}]
    )
    client.mutate(
        SLUG,
        [
            {"op": "set", "id": "visible-scope", "by": "swarm", "path": "tasks/t1/out_of_scope", "value": True},
            {"op": "set", "id": "in-scope", "by": "swarm", "path": "tasks/t1/out_of_scope", "value": False},
        ],
    )
    identity = {
        "id": "t1",
        "state": "claimed",
        "claimed_by": "api-reader",
        "issue_url": "https://github.com/example/repo/issues/1",
        "pr_url": "https://github.com/example/repo/pull/1",
        "done": False,
        "out_of_scope": False,
    }
    fields = {key: value for key, value in identity.items() if key not in ("id", "done", "out_of_scope")}
    fields["proof"] = {"output": "x" * MAX_REPLY}
    reply = client.mutate(
        SLUG, [{"op": "task_update", "id": "big-proof", "by": "swarm", "item": "tasks/t1", "fields": fields}]
    )
    assert len(json.dumps(reply, ensure_ascii=False).encode()) <= MAX_REPLY
    assert reply["applied"] == ["big-proof"]
    assert reply["tasks"] == [identity]
    exported = client.request(SLUG, "export", {})["data"]
    assert len(exported["tasks"][0]["proof"]["output"]) == MAX_REPLY


def test_metadata_revision_stays_independent_of_other_resources(live):
    before = request(live, "GET", "metadata")[1]
    rev = request(live, "GET", "chat")[1]["revision"]
    payload = {
        "ops": [{"op": "add", "id": "metadata-independent", "thread": "chat", "text": "Another resource"}],
        "guards": {"chat": rev},
    }
    assert request(live, "POST", "operations", payload)[0] == 200
    after = request(live, "GET", "metadata")[1]
    assert before["revision"] == after["revision"]
    assert before["data"]["_meta"]["rev"] < after["data"]["_meta"]["rev"]


def test_page_budget_counts_the_envelope(live, monkeypatch):
    from scripts.swarm_ledger.api.resources import MAX_REPLY

    row = {"id": "large", "description": "x" * (MAX_REPLY // 2 - 150)}
    document = {"tasks": [row, row], "_meta": {"rev": 1}}
    monkeypatch.setattr(server.repository, "get_document", lambda slug, **kwargs: document)
    status, reply = request(live, "GET", "tasks?limit=100")
    assert status == 200
    assert len(json.dumps(reply, ensure_ascii=False).encode()) <= MAX_REPLY
    assert len(reply["data"]) == 1
    assert reply["next_cursor"] is not None


def test_control_actions_validate_before_domain_execution(live):
    from unittest.mock import patch

    with patch.object(server, "swarm_control", return_value=({}, None)) as control:
        assert request(live, "POST", "swarm/actions", {"action": "pause"}) == (
            200,
            {"action": "pause", "accepted": True},
        )
        assert control.call_args.args[1] == ["pause"]
    for payload in ({"action": "set", "max_eng": True}, {"action": "terminate", "name": "-bad"}, {"action": "set"}):
        with patch.object(server, "swarm_control") as control:
            status, error = request(live, "POST", "swarm/actions", payload)
            assert status == 400
            assert error["error"]["code"] == "schema_invalid"
            control.assert_not_called()


def test_item_revision_uses_canonical_utf8_content(live, monkeypatch):
    document = {"tasks": [{"title": "Café", "id": "t1"}], "_meta": {"rev": 1}}
    monkeypatch.setattr(server.repository, "read", lambda slug, *keys: document)
    assert request(live, "GET", "tasks/t1") == (
        200,
        {
            "data": {"title": "Café", "id": "t1"},
            "revision": "e483879a300a6dae1421b8afa9f16094b2dec361357e663177417d89574b158f",
        },
    )


def test_metadata_resource_reports_its_named_fields(live, monkeypatch):
    document = {
        "title": "Report",
        "size": "swarm",
        "overview": "Intent",
        "orchestrator": "boss",
        "chat_instructions": "Speak plainly",
        "policy": {"control": "review"},
        "time_left_minutes": 42,
        "closed_at": 100,
        "summary": "Summary",
        "_meta": {"rev": 7, "updated_at": 200, "created_at": 10, "size": "swarm", "seed_error": None},
    }
    monkeypatch.setattr(server.repository, "get_document", lambda slug, **kwargs: document)
    status, reply = request(live, "GET", "metadata")
    assert status == 200
    assert reply["data"] == document
    assert len(reply["revision"]) == 64


def test_forbidden_error_envelope_keeps_its_status_and_bounds(live, monkeypatch):
    from scripts.swarm_ledger.api.resources import MAX_REPLY
    from tests.swarm_ledger.test_ledger_authority import authority

    document = {"tasks": [], "_meta": {"warnings": ["x" * 3000] * 100}}
    monkeypatch.setattr(server.repository, "get_document", lambda slug, **kwargs: document)
    headers = {
        "X-Ledger-Agent": "api-reader",
        "X-Ledger-Token": authority.agent_token(live["admin"], SLUG, "api-reader"),
    }
    status, reply = request(
        live,
        "POST",
        "operations",
        {
            "ops": [{"op": "add", "id": "denied-bound", "thread": "chat", "by": "other", "text": "Forbidden"}],
            "guards": {},
        },
        **headers,
    )
    assert status == 403
    assert len(json.dumps(reply, ensure_ascii=False).encode()) <= MAX_REPLY
    assert reply["error"]["code"] == "forbidden"
    assert reply["error"]["details"]["rejected"] == ["denied-bound"]
    assert reply["error"]["message"] == "Caller cannot perform this operation as its author"
    assert reply["error"]["details"]["_meta"]["warnings"] == ["x" * 1000] * 10 + ["api-reader cannot write as other"]


def test_request_errors_have_stable_envelopes(live):
    cases = [
        ("GET", "missing", None, {}, 404, "resource_missing", "No such resource"),
        ("GET", "metadata", None, {"Host": "untrusted.invalid"}, 403, "forbidden", "Host not allowed"),
        ("GET", "metadata", None, {"Origin": "https://untrusted.invalid"}, 403, "forbidden", "Origin not allowed"),
        (
            "GET",
            "metadata",
            None,
            {"X-Ledger-Token": "invalid"},
            403,
            "forbidden",
            "Missing or wrong ledger credential",
        ),
        (
            "POST",
            "operations",
            {},
            {"Content-Type": "text/plain"},
            415,
            "content_type",
            "Content-Type must be application/json",
        ),
        (
            "POST",
            "operations",
            {"ops": "invalid"},
            {},
            400,
            "schema_invalid",
            "Request does not match the resource schema",
        ),
        ("GET", "chat?limit=invalid", None, {}, 400, "schema_invalid", "Limit must be an integer"),
        ("GET", "chat?limit=1&limit=2", None, {}, 400, "schema_invalid", "Query parameters must be unique"),
    ]
    for method, resource, payload, headers, status, code, message in cases:
        assert request(live, method, resource, payload, **headers) == (
            status,
            {"error": {"code": code, "message": message}},
        )


def test_layout_write_and_bin_lifecycle_are_versioned(live):
    status, data, _ = send(
        live,
        "PUT",
        "/api/v1/layout",
        json.dumps({"capacity-box": {"height": 300}}).encode(),
        **{"Origin": sorted(server.ALLOWED_ORIGINS)[0], "Content-Type": "application/json"},
    )
    assert status == 200
    assert json.loads(data)["data"] == {"capacity-box": {"height": 300}}
    assert json.loads(send(live, "GET", "/api/v1/layout")[1]) == json.loads(data)
    for action in ("delete", "restore"):
        status, data, _ = send(
            live,
            "POST",
            "/api/v1/bin/actions",
            json.dumps({"action": action, "slug": SLUG}).encode(),
            **{"Origin": sorted(server.ALLOWED_ORIGINS)[0], "Content-Type": "application/json"},
        )
        assert status == 200
        assert json.loads(data) == {"slug": SLUG, "action": action}
    status, data, _ = send(live, "GET", f"/api/v1/ledgers/{SLUG}", **{"X-Ledger-Token": live["admin"]})
    assert status == 200
    assert json.loads(data)["data"]["title"] == "Authority"


def test_put_operations_and_options_keep_the_versioned_transport(live):
    import http.client

    rev = request(live, "GET", "chat")[1]["revision"]
    payload = {
        "ops": [{"op": "add", "id": "put-proof", "thread": "chat", "text": "Put canary"}],
        "guards": {"chat": rev},
    }
    assert request(live, "PUT", "operations", payload)[0] == 200
    conn = http.client.HTTPConnection("127.0.0.1", live["port"], timeout=5)
    try:
        conn.request(
            "OPTIONS",
            f"/api/v1/ledgers/{SLUG}/operations",
            headers={"Host": live["host"], "Origin": "null", "Access-Control-Request-Private-Network": "true"},
        )
        response = conn.getresponse()
        assert response.status == 204
        assert response.getheader("Access-Control-Allow-Origin") == "null"
        assert response.getheader("Access-Control-Allow-Methods") == "GET, PUT, POST, PATCH"
        assert (
            response.getheader("Access-Control-Allow-Headers")
            == "Content-Type, X-Ledger-Token, X-Ledger-Agent, X-Ledger-Slug"
        )
        assert response.getheader("Access-Control-Allow-Private-Network") == "true"
        assert [row for row in response.getheaders() if row[0].lower().startswith("access-control")] == [
            ("Access-Control-Allow-Origin", "null"),
            ("Access-Control-Allow-Methods", "GET, PUT, POST, PATCH"),
            ("Access-Control-Allow-Headers", "Content-Type, X-Ledger-Token, X-Ledger-Agent, X-Ledger-Slug"),
            ("Access-Control-Allow-Private-Network", "true"),
        ]
        assert response.read() == b""
    finally:
        conn.close()


def test_command_export_audit_and_purge_use_resources(live, capsys):
    from types import SimpleNamespace

    from tests.swarm_ledger.test_ledger_authority import ledger

    assert ledger.export(SLUG, service=True)["phases"][0]["id"] == "p1"
    ledger.cmd_audit(SimpleNamespace(slug=SLUG))
    assert "to clean" in capsys.readouterr().out
    ledger.cmd_artifact_purge(SimpleNamespace(slug=SLUG, name="api-reader"))
    assert json.loads(capsys.readouterr().out) == {"purged": 0, "artifacts": 0}


def test_domain_operation_families_use_their_named_guards(live):
    from scripts.swarm_ledger.api.client import ResourceClient
    from tests.swarm_ledger.test_ledger_authority import ledger

    client = ResourceClient(ledger.BASE, ledger.credentials(SLUG, service=True))
    client.mutate(SLUG, [{"op": "join", "id": "master-join", "by": "boss", "role": "orchestrator"}])
    client.mutate(
        SLUG,
        [
            {
                "op": "task_add",
                "id": "task-seed",
                "by": "boss",
                "task": "t1",
                "title": "Proof",
                "lane": "eng",
                "phase": "p1",
            }
        ],
    )
    client.mutate(SLUG, [{"op": "add_item", "id": "q1", "by": "boss", "list": "questions", "text": "Question"}])
    client.mutate(SLUG, [{"op": "add_item", "id": "f1", "by": "boss", "list": "followups", "text": "Follow up"}])
    cases = [
        ("phases/p1", {"op": "claim", "by": "api-reader", "item": "phases/p1"}),
        ("questions", {"op": "add_item", "by": "swarm", "list": "questions", "text": "Another question"}),
        ("metadata", {"op": "gate_bypass", "by": "api-reader", "unhandled": 0}),
        ("metadata", {"op": "gate_lift", "by": "api-reader", "gate": "watch"}),
        ("members", {"op": "ack", "by": "api-reader", "rev": 1}),
        ("members", {"op": "leave", "by": "api-reader"}),
        ("members", {"op": "agent_rename", "by": "swarm", "old": "boss", "new": "boss-new"}),
        ("metadata", {"op": "sync"}),
        ("metadata", {"op": "stats_sync"}),
        ("metadata", {"op": "title_set", "text": "Resource title"}),
        ("metadata", {"op": "summary_set", "by": "swarm", "note": "Summary"}),
        ("metadata", {"op": "size_set", "by": "swarm", "size": "swarm"}),
        ("metadata", {"op": "set", "by": "swarm", "path": "time_left_minutes", "value": 20}),
        ("sources", {"op": "source_add", "by": "swarm", "source": "https://example.com/proof"}),
        ("tasks/t1", {"op": "task_update", "by": "swarm", "item": "tasks/t1", "fields": {"description": "Updated"}}),
        ("phases/p1", {"op": "phase_update", "by": "swarm", "item": "phases/p1", "fields": {"description": "Updated"}}),
        ("phases", {"op": "phase_add", "by": "swarm", "phase": "p2", "title": "Later"}),
        ("questions/q1", {"op": "retext", "by": "swarm", "item": "questions/q1", "text": "New question"}),
        ("questions/q1", {"op": "answer", "by": "swarm", "item": "questions/q1", "text": "Answer"}),
        ("followups/f1", {"op": "set", "by": "swarm", "path": "followups/f1/done", "value": True}),
        ("followups/f1", {"op": "verdict", "item": "followups/f1", "verdict": "approved"}),
        ("priorities", {"op": "priority", "by": "swarm", "item": "questions/q1", "text": "Decide"}),
        ("priorities", {"op": "priority_clear", "target": "all"}),
        ("notifications", {"op": "notification_clear", "target": "all"}),
    ]
    for target, operation in cases:
        operation["id"] = uuid.uuid4().hex
        expected = request(live, "GET", target)[1]["revision"]
        status, reply = request(live, "POST", "operations", {"ops": [operation], "guards": {target: expected}})
        assert status == 200, operation["op"]
        assert "rejected" in reply


def test_resource_reads_preserve_pinned_identity(live):
    from unittest.mock import patch

    from tests.swarm_ledger.test_ledger_authority import ledger, pinned

    original = ledger.urllib.request.urlopen
    with pinned("api-reader"), patch.object(ledger.urllib.request, "urlopen", wraps=original) as opened:
        assert ledger.resource(SLUG, "metadata")["title"] == "Authority"
        outgoing = opened.call_args.args[0]
        assert outgoing.get_header("X-ledger-agent") == "api-reader"
        assert outgoing.get_header("X-ledger-token") != live["admin"]


def test_swarm_control_families_keep_domain_arguments(live):
    from unittest.mock import patch

    cases = [
        ({"action": "terminate", "name": "engineer"}, ["terminate", "engineer"], "swarm"),
        (
            {"action": "restore-decision", "agent": "engineer", "choice": "fresh"},
            ["restore-decision", "engineer", "fresh"],
            "swarm",
        ),
        ({"action": "lift", "agent": "engineer", "gate": "watch"}, ["lift", "engineer", "watch"], "swarm"),
        ({"action": "doctor_start"}, ["start"], "doctor"),
        ({"action": "doctor_stop"}, ["stop"], "doctor"),
        (
            {
                "action": "set",
                "max_eng": 2,
                "max_ci": 3,
                "max_plan": 1,
                "compact_limit": 600,
                "effort_min": "medium",
                "effort_max": "high",
                "autonomy": "full",
            },
            [
                "set",
                "max-eng-agents=2",
                "max-ci-agents=3",
                "max-plan-agents=1",
                "compact-limit=600",
                "effort-min=medium",
                "effort-max=high",
                "autonomy=full",
            ],
            "swarm",
        ),
    ]
    for payload, args, command in cases:
        with patch.object(server, "swarm_control", return_value=({}, None)) as control:
            assert request(live, "POST", "swarm/actions", payload) == (
                200,
                {"action": payload["action"], "accepted": True},
            )
            assert control.call_args.args == (SLUG, args, command)
    with patch.object(server, "refresh_quota", return_value=({}, None)) as refresh:
        assert request(live, "POST", "swarm/actions", {"action": "quota_refresh"}) == (
            200,
            {"action": "quota_refresh", "accepted": True},
        )
        refresh.assert_called_once_with(SLUG)
    with patch.object(server, "swarm_control", return_value=(None, "Failure")):
        assert request(live, "POST", "swarm/actions", {"action": "pause"}) == (
            502,
            {"error": {"code": "control_failed", "message": "Swarm control failed"}},
        )


def test_artifact_collection_and_lifecycle_keep_domain_state(live, tmp_path):
    from scripts.swarm_ledger.api.client import ResourceClient
    from tests.swarm_ledger.test_ledger_authority import ledger

    client = ResourceClient(ledger.BASE, ledger.credentials(SLUG, service=True))
    client.mutate(
        SLUG,
        [
            {
                "op": "task_add",
                "id": "artifact-task",
                "by": "swarm",
                "task": "t1",
                "title": "Proof",
                "lane": "eng",
                "artifact": True,
            }
        ],
    )
    client.mutate(
        SLUG,
        [
            {
                "op": "task_update",
                "id": "artifact-claim",
                "by": "swarm",
                "item": "tasks/t1",
                "fields": {"state": "claimed", "claimed_by": "api-reader"},
            }
        ],
    )
    source = tmp_path / "proof.md"
    source.write_text("# Requested proof")
    file = ledger.upload_artifact(SLUG, "api-reader", str(source), {"task": "t1", "title": "Proof"})
    published = {
        "op": "artifact_add",
        "id": "published-artifact",
        "by": "api-reader",
        "task": "t1",
        "title": "Proof",
        "file": file,
    }
    assert client.mutate(SLUG, [published])["rejected"] == []
    assert client.request(SLUG, "artifacts/published-artifact")["data"]["title"] == "Proof"
    assert (
        client.mutate(SLUG, [{"op": "artifact_delete", "id": "remove-artifact", "target": "published-artifact"}])[
            "rejected"
        ]
        == []
    )
    assert client.collection(SLUG, "artifacts") == []
    assert client.collection(SLUG, "artifact_trash")[0]["id"] == "published-artifact"
    assert (
        client.mutate(SLUG, [{"op": "artifact_restore", "id": "restore-artifact", "target": "published-artifact"}])[
            "rejected"
        ]
        == []
    )
    assert len(client.collection(SLUG, "artifacts")) == 1


def test_notes_question_answers_members_and_counts_are_resources(live):
    from scripts.swarm_ledger.api.client import ResourceClient
    from tests.swarm_ledger.test_ledger_authority import ledger

    client = ResourceClient(ledger.BASE, ledger.credentials(SLUG, service=True))
    client.mutate(SLUG, [{"op": "add", "id": "n1", "thread": "notes", "text": "Operator note"}])
    client.mutate(SLUG, [{"op": "add", "id": "nc1", "thread": "notes/n1/comments", "text": "Note comment"}])
    client.mutate(SLUG, [{"op": "add_item", "id": "q1", "by": "swarm", "list": "questions", "text": "Question"}])
    client.mutate(SLUG, [{"op": "add", "id": "a1", "thread": "questions/q1/answers", "text": "Operator answer"}])
    assert client.request(SLUG, "notes/n1")["data"]["text"] == "Operator note"
    assert client.collection(SLUG, "notes/n1/comments")[0]["text"] == "Note comment"
    assert client.collection(SLUG, "questions/q1/answers")[0]["text"] == "Operator answer"
    assert client.request(SLUG, "members/api-reader")["data"]["role"] == "member"
    counts = client.request(SLUG, "counts")["data"]
    assert counts["notes"] == 1
    assert counts["questions"] == 1
    assert request(live, "GET", "notes/missing")[0] == 404
    assert request(live, "GET", "notes/n1/unsupported")[0] == 404
    assert request(live, "GET", "members/missing")[0] == 404


def test_central_mutation_schema_boundaries_leave_state_unchanged(live):
    before = request(live, "GET", "metadata")
    operation = {"op": "sync", "id": "sync-proof"}
    invalid = [
        {"ops": [], "guards": {}},
        {"ops": [operation], "guards": []},
        {"ops": [operation], "guards": {"metadata": "invalid"}},
        {"ops": [operation], "guards": {}, "unexpected": True},
        {"ops": [operation] * 101, "guards": {}},
        {"ops": [{**operation, "id": ""}], "guards": {}},
        {"ops": [{**operation, "id": "x" * 201}], "guards": {}},
        {"ops": [{"op": "sync"}], "guards": {}},
        {"ops": [{**operation, "id": 1}], "guards": {}},
        {"ops": [{"op": "join", "id": "joined", "by": 1}], "guards": {}},
        {"ops": [operation], "guards": {}, "changes": [{"path": "phases/p1/done", "value": True}]},
        {"ops": [operation], "guards": {}, "id": "sync-proof", "changes": [{"path": "phases/p1/done", "value": True}]},
        {"ops": [], "guards": {}, "id": "change", "changes": [{"path": "phases/p1/done", "base": 1, "value": True}]},
        {"ops": [], "guards": {}, "id": "change", "changes": [{"path": "unsupported", "value": True}]},
    ]
    for index, payload in enumerate(invalid):
        expected = "Request does not match the resource schema"
        if index == 0:
            expected = "At least one operation is required"
        if index in (5, 6, 7, 8):
            expected = "Operation sync does not match its schema at field id"
        if index == 9:
            expected = "Operation join does not match its schema at field by"
        if index in (10, 11):
            expected = "Checkbox changes need a distinct operation identifier"
        assert request(live, "POST", "operations", payload) == (
            400,
            {"error": {"code": "schema_invalid", "message": expected}},
        )
    assert request(live, "POST", "operations", {"ops": [{"op": "unknown", "id": "unknown"}], "guards": {}}) == (
        400,
        {"error": {"code": "schema_invalid", "message": "Unknown operation"}},
    )
    assert request(
        live, "POST", "operations", {"ops": [{"op": "title_set", "id": "empty-title", "text": ""}], "guards": {}}
    ) == (
        400,
        {
            "error": {
                "code": "schema_invalid",
                "message": "Operation does not match its domain schema: title_set needs a title of 1 to 200 characters",
            }
        },
    )
    assert request(live, "GET", "metadata") == before


def test_client_transport_retains_timeout_and_json_headers(live):
    from unittest.mock import patch

    from scripts.swarm_ledger.api.client import ResourceClient
    from tests.swarm_ledger.test_ledger_authority import ledger

    client = ResourceClient(ledger.BASE, ledger.credentials(SLUG, service=True))
    original = ledger.urllib.request.urlopen
    with patch.object(ledger.urllib.request, "urlopen", wraps=original) as opened:
        assert client.request(SLUG, "metadata")["data"]["title"] == "Authority"
        assert opened.call_args.kwargs["timeout"] == 10
        outgoing = opened.call_args.args[0]
        assert outgoing.get_header("Content-type") == "application/json"
        assert outgoing.get_method() == "GET"
        assert outgoing.data is None
        with pytest.raises(ledger.urllib.error.HTTPError) as error:
            client.request(f"{SLUG}/chat", "metadata")
        assert error.value.code == 404
        assert json.loads(error.value.read()) == {"error": {"code": "ledger_missing", "message": "No such ledger"}}
        assert opened.call_args.args[0].full_url == f"{ledger.BASE}/api/v1/ledgers/{SLUG}%2Fchat/metadata"


def test_operation_string_and_identifier_limits_are_central(live):
    payloads = [
        {"ops": [{"op": "title_set", "id": "long-title", "text": "x" * 100001}], "guards": {}},
        {"operation_id": "", "ops": [{"op": "sync", "id": "sync"}], "guards": {}},
        {"operation_id": "x" * 201, "ops": [{"op": "sync", "id": "sync"}], "guards": {}},
    ]
    for index, payload in enumerate(payloads):
        message = "Request does not match the resource schema"
        if index == 0:
            message = "Operation title_set does not match its schema at field text"
        assert request(live, "POST", "operations", payload) == (
            400,
            {"error": {"code": "schema_invalid", "message": message}},
        )


def test_body_size_and_json_rejection_are_stable(live):
    path = f"/api/v1/ledgers/{SLUG}/export"
    headers = {"X-Ledger-Token": live["admin"], "Content-Type": "application/json"}
    for content, extra in (
        (b"{", {}),
        (b"{}", {"Content-Length": "-1"}),
        (b"{}", {"Content-Length": str(server.MAX_BODY + 1)}),
    ):
        status, data, _ = send(live, "POST", path, content, **{**headers, **extra})
        assert (status, json.loads(data)) == (
            400,
            {"error": {"code": "schema_invalid", "message": "Request body must be a bounded JSON object"}},
        )
    content = b"{}" + b" " * (server.MAX_BODY - 2)
    status, data, _ = send(live, "POST", path, content, **headers)
    assert status == 200
    assert json.loads(data)["data"]["title"] == "Authority"


def test_versioned_logs_exclude_query_and_bearer(live, capsys):
    from datetime import datetime
    from unittest.mock import patch

    clock = datetime(2026, 10, 7, 9, 0, 0)
    capsys.readouterr()
    original = server.time.strftime
    with patch.object(server.time, "strftime", side_effect=lambda fmt, *args: original(fmt, clock.timetuple())):
        status, _ = request(
            live, "GET", "metadata?token=private-canary?tail", **{"X-Ledger-Token": "credential-canary"}
        )
    assert status == 403
    logged = capsys.readouterr().err
    assert logged == f"09:00:00 GET /api/v1/ledgers/{SLUG}/metadata\n"
    assert "private-canary" not in logged
    assert "credential-canary" not in logged


def test_upload_refusals_remain_json_and_do_not_mutate(live):
    from tests.swarm_ledger.test_ledger_authority import authority

    headers = {
        "X-Ledger-Agent": "api-reader",
        "X-Ledger-Token": authority.agent_token(live["admin"], SLUG, "api-reader"),
        "Content-Type": "application/octet-stream",
        "X-Artifact-Name": "proof.md",
    }
    before = request(live, "GET", "metadata")
    status, data, ctype = send(
        live,
        "POST",
        f"/api/v1/ledgers/{SLUG}/uploads/artifacts",
        b"# Proof",
        **{
            **headers,
            "Origin": sorted(server.ALLOWED_ORIGINS)[0],
            "X-Ledger-Agent": "missing",
            "X-Ledger-Token": authority.agent_token(live["admin"], SLUG, "missing"),
        },
    )
    assert status == 403
    assert ctype == "application/json"
    assert json.loads(data) == {
        "error": {"code": "forbidden", "message": "agent must join this ledger before uploading"}
    }
    status, data, ctype = send(live, "POST", f"/api/v1/ledgers/{SLUG}/uploads/media", b"invalid image", **headers)
    assert status == 415
    assert ctype == "application/json"
    assert json.loads(data)["error"]["code"] == "request_refused"
    assert request(live, "GET", "metadata") == before


@pytest.mark.parametrize("error", [OSError("Backend failure"), ValueError("Invalid stored task")])
def test_storage_errors_have_a_stable_bounded_envelope(live, monkeypatch, caplog, error):
    def unreadable(slug, **kwargs):
        raise error

    monkeypatch.setattr(server.repository, "get_document", unreadable)
    assert request(live, "GET", "metadata") == (
        500,
        {
            "error": {"code": "storage_error", "message": "Resource could not be read or written"},
        },
    )
    record = caplog.records[-1]
    assert record.name == "scripts.swarm_ledger.api.routes"
    assert record.levelname == "ERROR"
    assert record.getMessage() == "Ledger API storage failure"
    assert record.exc_info[1] is error


def test_worker_checkbox_refusal_preserves_the_resource(live):
    from tests.swarm_ledger.test_ledger_authority import authority

    before = request(live, "GET", "phases/p1")
    headers = {
        "X-Ledger-Agent": "api-reader",
        "X-Ledger-Token": authority.agent_token(live["admin"], SLUG, "api-reader"),
    }
    payload = {
        "id": "worker-checkbox",
        "ops": [],
        "changes": [{"path": "phases/p1/done", "value": True}],
        "guards": {"phases/p1": before[1]["revision"]},
    }
    assert request(live, "POST", "operations", payload, **headers) == (
        403,
        {
            "error": {"code": "forbidden", "message": "Checkbox changes need the operator"},
        },
    )
    assert request(live, "GET", "phases/p1") == before


def test_sdk_preserves_explicit_stale_revision(live):
    import urllib.error

    from scripts.swarm_ledger.api.client import ResourceClient
    from tests.swarm_ledger.test_ledger_authority import ledger

    client = ResourceClient(ledger.BASE, ledger.credentials(SLUG, service=True))
    stale = client.request(SLUG, "chat")["revision"]
    client.mutate(SLUG, [{"op": "add", "id": "newer-chat", "thread": "chat", "text": "Newer"}])
    operation = {"op": "add", "id": "stale-explicit", "thread": "chat", "text": "Stale", "expected_revision": stale}
    with pytest.raises(urllib.error.HTTPError) as error:
        client.mutate(SLUG, [operation])
    assert error.value.code == 409
    assert json.loads(error.value.read()) == {
        "error": {
            "code": "revision_conflict",
            "message": "Resource changed since the expected revision",
            "details": {"path": "chat"},
        }
    }
    assert operation["expected_revision"] == stale
    assert not any(row["id"] == "stale-explicit" for row in client.collection(SLUG, "chat"))


def test_composite_status_preserves_member_events_and_crew(live, capsys):
    from types import SimpleNamespace

    from tests.swarm_ledger.test_ledger_authority import ledger

    ledger.request(
        SLUG,
        [{"op": "add", "id": "addressed-event", "thread": "chat", "text": "@api-reader Please verify"}],
        service=True,
    )
    state = ledger.request(SLUG, service=True)
    member = state["_meta"]["members"]["api-reader"]
    assert member["role"] == "member"
    assert member["handled_rev"] == 1
    assert member["claims"] == []
    assert "id" not in member
    assert "revision" not in member
    assert member["last_seen"] > 0
    assert any(
        event.get("id") == "addressed-event" and event["text"] == "@api-reader Please verify"
        for event in state["_meta"]["events"]
    )
    [crew] = state["_meta"]["crew"]
    assert crew["name"] == "api-reader"
    assert crew["role"] == "member"
    assert crew["handled_rev"] == 1
    ledger.cmd_status(SimpleNamespace(slug=SLUG, name="api-reader"))
    status = json.loads(capsys.readouterr().out)
    assert status["crew"] == state["_meta"]["crew"]
    assert status["rev"] == state["_meta"]["rev"]
    assert status["unhandled"] == 3
    ledger.cmd_events(SimpleNamespace(slug=SLUG, name="api-reader"))
    assert "Please verify" in capsys.readouterr().out


def test_default_collection_limit_and_cursor_edges(live, monkeypatch):
    rows = [{"id": f"t{index}", "title": f"Task {index}"} for index in range(51)]
    document = {"tasks": rows, "_meta": {"rev": 1}}
    monkeypatch.setattr(server.repository, "get_document", lambda slug, **kwargs: document)
    status, first = request(live, "GET", "tasks")
    assert status == 200
    assert len(first["data"]) == 50
    assert first["data"][0]["id"] == "t0"
    assert first["data"][-1]["id"] == "t49"
    assert len(first["data"][0]["revision"]) == 64
    status, last = request(live, "GET", f"tasks?cursor={first['next_cursor']}")
    assert status == 200
    assert [row["id"] for row in last["data"]] == ["t50"]
    assert last["next_cursor"] is None
    status, beyond = request(live, "GET", f"tasks?cursor={first['revision']}:100")
    assert status == 200
    assert beyond["data"] == []
    assert beyond["next_cursor"] is None
    assert len(request(live, "GET", "tasks?limit=100")[1]["data"]) == 51


def test_source_collection_keeps_primitive_values(live):
    from scripts.swarm_ledger.api.client import ResourceClient
    from tests.swarm_ledger.test_ledger_authority import ledger

    client = ResourceClient(ledger.BASE, ledger.credentials(SLUG, service=True))
    client.mutate(
        SLUG, [{"op": "source_add", "id": "source-proof", "by": "swarm", "source": "https://example.com/proof"}]
    )
    assert client.collection(SLUG, "sources") == ["https://example.com/proof"]


def test_oversized_task_state_fields_fall_back_to_ack(live, monkeypatch):
    from scripts.swarm_ledger.api.resources import MAX_REPLY

    state = {
        "tasks": [{"id": "t1", "state": "claimed", "issue_url": "x" * MAX_REPLY}],
        "_meta": {"rev": 2, "warnings": []},
    }
    monkeypatch.setattr(server.repository, "apply_ops", lambda slug, **kwargs: (state, []))
    status, reply = request(
        live,
        "POST",
        "operations",
        {
            "ops": [
                {
                    "op": "task_update",
                    "id": "large-ack",
                    "by": "swarm",
                    "item": "tasks/t1",
                    "fields": {"state": "claimed"},
                }
            ],
            "guards": {"tasks/t1": "0" * 64},
        },
    )
    assert status == 200
    assert reply == {
        "applied": ["large-ack"],
        "rejected": [],
        "_meta": {"rev": 2, "warnings": []},
        "task_rows_omitted": True,
    }
    assert len(json.dumps(reply).encode()) <= MAX_REPLY


def test_alert_operations_use_guarded_resources(live):
    status, before = request(live, "GET", "alerts")
    assert status == 200
    assert before["data"]
    target = before["data"][0]["id"]
    for kind, fields in (("alert_claim", {}), ("alert_close", {"outcome": "Checked"})):
        payload = {
            "ops": [{"op": kind, "id": uuid.uuid4().hex, "target": target, **fields}],
            "guards": {"alerts": before["revision"]},
        }
        status, result = request(live, "POST", "operations", payload)
        assert status == 200
        assert result["rejected"] == []
        status, before = request(live, "GET", "alerts")
        assert status == 200
    row = next(row for row in before["data"] if row["id"] == target)
    assert row["state"] == "done"
    assert row["outcome"] == "Checked"


def test_global_summary_items_and_missing_resources(live):
    status, data, _ = send(live, "GET", "/api/v1/ledgers")
    assert status == 200
    assert SLUG in [row["slug"] for row in json.loads(data)["data"]]
    status, data, _ = send(
        live,
        "POST",
        "/api/v1/bin/actions",
        json.dumps({"action": "delete", "slug": SLUG}).encode(),
        **{"Origin": sorted(server.ALLOWED_ORIGINS)[0], "Content-Type": "application/json"},
    )
    assert status == 200
    status, data, _ = send(live, "GET", f"/api/v1/bin/{SLUG}")
    assert status == 200
    assert json.loads(data)["data"]["slug"] == SLUG
    status, data, _ = send(live, "GET", "/api/v1/ledgers")
    assert status == 200
    assert SLUG not in [row["slug"] for row in json.loads(data)["data"]]
    for path, message in (
        ("/api/v1/bin/absent", "No such summary"),
        ("/api/v1/missing", "No such resource"),
        ("/api/v1/bin/extra/path", "No such resource"),
    ):
        status, data, _ = send(live, "GET", path)
        assert (status, json.loads(data)) == (404, {"error": {"code": "resource_missing", "message": message}})


def test_named_swarm_export_and_missing_operations(live, monkeypatch):
    state = {"slug": SLUG, "agents": [{"name": "engineer", "detail": "x" * 300000}], "mode": "running"}
    monkeypatch.setattr(server, "swarm_status", lambda slug: state)
    assert request(live, "POST", "swarm/export", {}) == (200, {"data": state})
    assert request(live, "GET", "swarm/agents")[0] == 413
    assert request(live, "GET", "swarm/missing") == (
        404,
        {"error": {"code": "resource_missing", "message": "No such swarm resource"}},
    )
    monkeypatch.setattr(server, "swarm_status", lambda slug: None)
    for method, path, payload in (("POST", "swarm/export", {}), ("GET", "swarm", None)):
        assert request(live, method, path, payload) == (
            404,
            {"error": {"code": "swarm_missing", "message": "No swarm for this ledger"}},
        )
    assert request(live, "POST", "missing-operation", {}) == (
        404,
        {"error": {"code": "resource_missing", "message": "No such operation resource"}},
    )


def test_operator_doctor_phrase_keeps_the_ledger(live, monkeypatch):
    from unittest.mock import Mock

    doctor = Mock()
    monkeypatch.setattr(server, "doctor_phrase", doctor)
    revision = request(live, "GET", "chat")[1]["revision"]
    payload = {
        "ops": [{"op": "add", "id": "doctor-stop", "thread": "chat", "text": server.DOCTOR_PHRASE}],
        "guards": {"chat": revision},
    }
    status, result = request(live, "POST", "operations", payload)
    assert status == 200
    doctor.assert_called_once()
    slug, state = doctor.call_args.args
    assert slug == SLUG
    assert state["_meta"]["rev"] == result["_meta"]["rev"]
    assert state["chat"][-1]["text"] == server.DOCTOR_PHRASE


def test_mutations_enforce_the_ledger_talk_budget(live, monkeypatch):
    import fakeredis

    from scripts.gates import progress, talk
    from scripts.swarm.store import RedisStore, SwarmConfig

    redis = fakeredis.FakeRedis(decode_responses=True)
    store = RedisStore(redis)
    store.create(SwarmConfig(SLUG, "repo", 1, 1))
    store.update(SLUG, gates={"talk": "enforce"})
    worker = "engineer@323133-0440"
    marks = progress.Progress(redis, SLUG)
    marks.outcome(worker, "pushed", 1)
    for _ in range(talk.BUDGET):
        marks.talk(worker)

    def budget(gate, slug):
        _talk_init(gate, slug, connect=lambda: redis)

    monkeypatch.setattr(talk.Budget, "_marks", _talk_marks)
    monkeypatch.setattr(talk.Budget, "__init__", budget)
    revision = request(live, "GET", "chat")[1]["revision"]
    payload = {
        "ops": [{"op": "add", "id": "budget-denied", "thread": "chat", "text": "Beyond the budget", "by": worker}],
        "guards": {"chat": revision},
    }
    status, result = request(live, "POST", "operations", payload)
    assert status == 200
    assert result["applied"] == []
    assert result["rejected"] == ["budget-denied"]
    assert result["_meta"]["warnings"] == [
        "phase p1 description has 101 words, limit 100",
        talk.refusal(worker, talk.BUDGET, SLUG),
    ]
    assert request(live, "GET", "chat")[1]["revision"] == revision
    assert marks.read(worker).talk == talk.BUDGET


def test_uploaded_media_is_resolved_in_the_mutation_thread(live):
    from io import BytesIO

    from PIL import Image

    media = BytesIO()
    Image.new("RGB", (2, 3), "white").save(media, format="PNG")
    status, data, _ = send(
        live,
        "POST",
        f"/api/v1/ledgers/{SLUG}/uploads/media",
        media.getvalue(),
        **{
            "X-Ledger-Token": live["admin"],
            "Content-Type": "application/octet-stream",
            "X-Artifact-Name": "proof.png",
            "Origin": sorted(server.ALLOWED_ORIGINS)[0],
        },
    )
    assert status == 200
    descriptor = json.loads(data)
    revision = request(live, "GET", "phases/p1/comments")[1]["revision"]
    payload = {
        "ops": [
            {
                "op": "add",
                "id": "media-comment",
                "thread": "phases/p1/comments",
                "text": "Image",
                "attachments": [{"id": descriptor["id"]}],
            }
        ],
        "guards": {"phases/p1/comments": revision},
    }
    status, result = request(live, "POST", "operations", payload)
    assert status == 200
    assert result["applied"] == ["media-comment"]
    attachments = request(live, "GET", "phases/p1/comments")[1]["data"][0]["attachments"]
    assert [{key: row[key] for key in descriptor} for row in attachments] == [descriptor]
    assert attachments[0]["width"] == 2
    assert attachments[0]["height"] == 3


def test_metadata_guard_changes_only_with_its_content(live):
    before = request(live, "GET", "metadata")[1]
    payload = {
        "ops": [{"op": "title_set", "id": "title-first", "text": "Changed"}],
        "guards": {"metadata": before["revision"]},
    }
    assert request(live, "POST", "operations", payload)[0] == 200
    after = request(live, "GET", "metadata")[1]
    assert after["revision"] != before["revision"]
    payload = {
        "ops": [{"op": "title_set", "id": "title-stale", "text": "Lost"}],
        "guards": {"metadata": before["revision"]},
    }
    assert request(live, "POST", "operations", payload) == (
        409,
        {
            "error": {
                "code": "revision_conflict",
                "message": "Resource changed since the expected revision",
                "details": {"path": "metadata"},
            }
        },
    )
    assert request(live, "GET", "metadata")[1]["data"]["title"] == "Changed"


def test_one_resource_guard_covers_a_batch_and_its_retry(live):
    revision = request(live, "GET", "chat")[1]["revision"]
    payload = {
        "ops": [
            {"op": "add", "id": f"batch-{index}", "thread": "chat", "text": f"Batch {index}"} for index in range(2)
        ],
        "guards": {"chat": revision},
    }
    first = request(live, "POST", "operations", payload)
    assert first[0] == 200
    assert first[1]["applied"] == ["batch-0", "batch-1"]
    assert first[1]["rejected"] == []
    assert request(live, "POST", "operations", payload) == first
    assert [row["text"] for row in request(live, "GET", "chat")[1]["data"]][-2:] == ["Batch 0", "Batch 1"]


def test_exact_error_detail_limits():
    from scripts.swarm_ledger.api.errors import APIError

    warnings = [f"{index}:" + "w" * 1500 for index in range(22)]
    error = APIError(403, "forbidden", "m" * 1200, {"rejected": ["request"], "_meta": {"warnings": warnings}})
    assert error.envelope() == {
        "error": {
            "code": "forbidden",
            "message": "m" * 1000,
            "details": {"rejected": ["request"], "_meta": {"warnings": [item[:1000] for item in warnings[-20:]]}},
        }
    }
    error = APIError(403, "forbidden", "Refused", {"rejected": ["request"], "optional": "x" * 300000})
    assert error.envelope() == {
        "error": {"code": "forbidden", "message": "Refused", "details": {"rejected": ["request"]}}
    }


def test_duplicate_ids_and_short_ids_keep_the_schema(live):
    before = request(live, "GET", "metadata")[1]
    payload = {
        "ops": [{"op": "sync", "id": "same"}, {"op": "sync", "id": "same"}],
        "guards": {"metadata": before["revision"]},
    }
    assert request(live, "POST", "operations", payload) == (
        400,
        {"error": {"code": "schema_invalid", "message": "Operation identifiers must be distinct"}},
    )
    assert request(live, "GET", "metadata")[1] == before
    payload = {"operation_id": "a", "ops": [{"op": "sync", "id": "a"}], "guards": {"metadata": before["revision"]}}
    status, result = request(live, "POST", "operations", payload)
    assert status == 200
    assert result["applied"] == ["a"]
    for path in ("chat?limit=", "chat?cursor="):
        assert request(live, "GET", path)[0] == 400


def test_layout_schema_and_bin_refusals_preserve_state(live, monkeypatch):
    origin = {"Origin": sorted(server.ALLOWED_ORIGINS)[0], "Content-Type": "application/json"}
    initial = json.loads(send(live, "GET", "/api/v1/layout")[1])
    for payload in (
        {"capacity-box": {}},
        {"capacity-box": []},
        {"capacity-box": None},
        {"capacity-box": "invalid"},
        {"capacity-box": {"height": True}},
        {"capacity-box": {"unknown": 1}},
        {"unknown": {"height": 1}},
    ):
        status, data, _ = send(live, "PUT", "/api/v1/layout", json.dumps(payload).encode(), **origin)
        assert (status, json.loads(data)) == (
            400,
            {"error": {"code": "schema_invalid", "message": "Request does not match the resource schema"}},
        )
        assert json.loads(send(live, "GET", "/api/v1/layout")[1]) == initial
    status, data, _ = send(live, "PUT", "/api/v1/layout", b'{"capacity-box":{"height":-1}}', **origin)
    assert (status, json.loads(data)) == (
        400,
        {"error": {"code": "schema_invalid", "message": "Layout sizes are out of range"}},
    )
    for payload in (
        {},
        {"action": "delete"},
        {"slug": SLUG},
        {"action": "invalid", "slug": SLUG},
        {"action": "delete", "slug": SLUG, "extra": True},
    ):
        status, data, _ = send(live, "POST", "/api/v1/bin/actions", json.dumps(payload).encode(), **origin)
        assert (status, json.loads(data)) == (
            400,
            {"error": {"code": "schema_invalid", "message": "Request does not match the resource schema"}},
        )
    monkeypatch.setattr(server.ledger_bin, "restore", lambda slug: False)
    for payload, code, message in (
        ({"action": "delete", "slug": "absent"}, "ledger_missing", "No such ledger"),
        ({"action": "restore", "slug": SLUG}, "resource_missing", "Ledger is not in the bin"),
    ):
        status, data, _ = send(live, "POST", "/api/v1/bin/actions", json.dumps(payload).encode(), **origin)
        assert (status, json.loads(data)) == (404, {"error": {"code": code, "message": message}})
    assert request(live, "GET", "metadata")[0] == 200


def test_bin_delete_stops_its_running_swarm(live, monkeypatch):
    from unittest.mock import Mock

    control = Mock(return_value=(None, "Stop failed"))
    monkeypatch.setattr(server, "swarm_status", lambda slug: {"mode": "running"})
    monkeypatch.setattr(server, "swarm_control", control)
    status, data, _ = send(
        live,
        "POST",
        "/api/v1/bin/actions",
        json.dumps({"action": "delete", "slug": SLUG}).encode(),
        **{"Origin": sorted(server.ALLOWED_ORIGINS)[0], "Content-Type": "application/json"},
    )
    assert (status, json.loads(data)) == (200, {"slug": SLUG, "action": "delete", "swarm_error": "Stop failed"})
    control.assert_called_once_with(SLUG, ["stop", "--now"])


def test_swarm_shapes_revisions_and_stale_cursor(live, monkeypatch):
    from scripts.swarm_ledger.api.resources import revision

    state = {"slug": SLUG, "running": True, "agents": [{"name": "one"}, {"name": "two"}]}
    monkeypatch.setattr(server, "swarm_status", lambda slug: state)
    assert request(live, "GET", "swarm") == (
        200,
        {
            "data": {"slug": SLUG, "running": True},
            "revision": revision({"slug": SLUG, "running": True}),
            "collections": ["agents"],
        },
    )
    status, first = request(live, "GET", "swarm/agents?limit=1")
    assert status == 200
    assert first["revision"] == revision(state["agents"])
    state["agents"][1]["name"] = "changed"
    assert request(live, "GET", f"swarm/agents?cursor={first['next_cursor']}") == (
        409,
        {"error": {"code": "revision_conflict", "message": "Collection changed; restart pagination"}},
    )


def test_item_ids_and_absent_threads(live):
    member = request(live, "GET", "members/api-reader")[1]["data"]
    assert member["id"] == "api-reader"
    assert request(live, "GET", "chat/seed-chat-one/missing") == (
        404,
        {"error": {"code": "resource_missing", "message": "No such resource"}},
    )
    revision = request(live, "GET", "tasks")[1]["revision"]
    payload = {
        "ops": [
            {"op": "task_add", "id": "empty-comments", "by": "swarm", "task": "t1", "title": "Empty", "lane": "eng"}
        ],
        "guards": {"tasks": revision},
    }
    assert request(live, "POST", "operations", payload)[0] == 200
    status, result = request(live, "GET", "tasks/t1/comments")
    assert status == 200
    assert result["data"] == []
    assert result["next_cursor"] is None


@pytest.mark.parametrize(
    "path,code,ctype,origin",
    [
        ("/api/v1/proof", 400, "text/plain", None),
        ("/api/v1/proof", 403, "text/plain", None),
        ("/api/v1/proof", 404, "text/plain", server.FILE_ORIGIN),
        ("/api/v1/proof", 399, "text/plain", None),
        ("/api/v1/proof", 400, "application/json", None),
        ("/api/proof", 403, "text/plain", server.FILE_ORIGIN),
    ],
)
def test_transport_preserves_status_json_and_legacy_errors(path, code, ctype, origin):
    from io import BytesIO
    from unittest.mock import Mock

    handler = server.Handler.__new__(server.Handler)
    handler.path = path
    handler.headers = {"Origin": origin}
    handler.wfile = BytesIO()
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()
    handler.send(code, "Refused é", ctype)
    data = handler.wfile.getvalue()
    handler.send_response.assert_called_once_with(code)
    converted = path.startswith("/api/v1/") and code >= 400 and ctype != "application/json"
    if converted:
        assert json.loads(data) == {
            "error": {"code": "forbidden" if code == 403 else "request_refused", "message": "Refused é"}
        }
    else:
        assert data == "Refused é".encode()
    expected = [
        ("Content-Type", "application/json" if converted else ctype),
        ("Content-Length", str(len(data))),
        ("Cache-Control", "no-store"),
    ]
    if origin == server.FILE_ORIGIN:
        expected.append(("Access-Control-Allow-Origin", server.FILE_ORIGIN))
    assert [call.args for call in handler.send_header.call_args_list] == expected
    handler.end_headers.assert_called_once_with()


@pytest.mark.parametrize("payload", [None, True, [], 5, "layout"])
def test_layout_body_must_be_an_object(live, payload):
    before = send(live, "GET", "/api/v1/layout")[1]
    status, data, _ = send(
        live,
        "PUT",
        "/api/v1/layout",
        json.dumps(payload).encode(),
        **{"Origin": sorted(server.ALLOWED_ORIGINS)[0], "Content-Type": "application/json"},
    )
    assert (status, json.loads(data)) == (
        400,
        {"error": {"code": "schema_invalid", "message": "Request does not match the resource schema"}},
    )
    assert send(live, "GET", "/api/v1/layout")[1] == before


def test_sdk_collections_and_tick_readers_cross_page_boundaries(live):
    from scripts.swarm.ledger_client import LedgerClient
    from scripts.swarm_ledger.api.client import ResourceClient
    from tests.swarm_ledger.test_ledger_authority import core, ledger

    core.sync(
        SLUG,
        ops=[{"op": "add", "id": f"page-{index}", "thread": "chat", "text": f"Page {index}"} for index in range(105)],
    )
    client = ResourceClient(ledger.BASE, ledger.credentials(SLUG, service=True))
    rows = client.collection(SLUG, "chat")
    assert len(rows) == 107
    assert [row["text"] for row in rows][-105:] == [f"Page {index}" for index in range(105)]
    assert LedgerClient(service=True).chat(SLUG) == rows
    events = client.collection(SLUG, "events")
    assert len(events) > 100
    assert LedgerClient(service=True).events(SLUG) == events
    core.sync(SLUG, ops=[{"op": "close", "id": "closed-proof", "by": "operator"}])
    assert LedgerClient(service=True).closed(SLUG) is True


def test_sdk_retains_generated_guards_and_operation_ids_for_retry(live):
    from scripts.swarm_ledger.api.client import ResourceClient
    from tests.swarm_ledger.test_ledger_authority import ledger

    client = ResourceClient(ledger.BASE, ledger.credentials(SLUG, service=True))
    before = request(live, "GET", "chat")[1]["revision"]
    operations = [{"op": "add", "id": "sdk-retry", "thread": "chat", "text": "Once"}]
    first = client.mutate(SLUG, operations)
    assert operations[0]["expected_revision"] == before
    assert operations[0]["operation_id"]
    saved = dict(operations[0])
    assert client.mutate(SLUG, operations) == first
    assert operations[0] == saved
    assert [row["id"] for row in client.collection(SLUG, "chat")].count("sdk-retry") == 1


def test_pinned_worker_sdk_preserves_forbidden_details(live):
    from scripts.swarm_ledger.api.client import ResourceClient
    from tests.swarm_ledger.test_ledger_authority import authority, ledger

    headers = {
        "X-Ledger-Agent": "api-reader",
        "X-Ledger-Token": authority.agent_token(live["admin"], SLUG, "api-reader"),
    }
    client = ResourceClient(ledger.BASE, headers)
    before = request(live, "GET", "chat")[1]
    operation = {
        "op": "add",
        "id": "sdk-forbidden",
        "by": "other",
        "thread": "chat",
        "text": "Refused",
        "expected_revision": before["revision"],
        "operation_id": "sdk-forbidden-request",
    }
    result = client.mutate(SLUG, [operation])
    payload = {
        "operation_id": operation["operation_id"],
        "ops": [{key: value for key, value in operation.items() if key not in ("expected_revision", "operation_id")}],
        "guards": {"chat": before["revision"]},
    }
    status, error = request(live, "POST", "operations", payload, **headers)
    assert status == 403
    assert result == error["error"]["details"]
    assert result["rejected"] == ["sdk-forbidden"]
    assert result["_meta"]["warnings"][-1] == "api-reader cannot write as other"
    assert request(live, "GET", "chat")[1] == before


def test_export_validates_its_body_and_hides_receipts(live, monkeypatch):
    from unittest.mock import Mock

    revision = request(live, "GET", "metadata")[1]["revision"]
    payload = {"id": "ordinary", "ops": [{"op": "sync", "id": "ordinary"}], "guards": {"metadata": revision}}
    status, result = request(live, "POST", "operations", payload)
    assert status == 200
    assert result["applied"] == ["ordinary"]
    for path in ("export", "swarm/export"):
        for body in (None, [], 7, {"extra": True}):
            status, data, _ = send(
                live,
                "POST",
                f"/api/v1/ledgers/{SLUG}/{path}",
                json.dumps(body).encode(),
                **{"X-Ledger-Token": live["admin"], "Content-Type": "application/json"},
            )
            assert (status, json.loads(data)) == (
                400,
                {"error": {"code": "schema_invalid", "message": "Request does not match the resource schema"}},
            )
    exported = request(live, "POST", "export", {})[1]["data"]
    assert "seeds" not in exported["_meta"]
    assert "api_operations" not in exported["_meta"]
    read = Mock(return_value={"slug": SLUG})
    monkeypatch.setattr(server, "swarm_status", read)
    assert request(live, "POST", "swarm/export", {}) == (200, {"data": {"slug": SLUG}})
    read.assert_called_once_with(SLUG)


def test_raw_upload_headers_reject_invalid_lengths_and_names(live):
    import http.client

    cases = [
        (None, "proof.png", "Request does not match the resource schema"),
        ("invalid", "proof.png", "Upload length must be an integer"),
        ("0", "proof.png", "Request does not match the resource schema"),
        ("1", "a" * 201, "Request does not match the resource schema"),
    ]
    for length, name, message in cases:
        connection = http.client.HTTPConnection("127.0.0.1", live["port"], timeout=5)
        try:
            connection.putrequest("POST", f"/api/v1/ledgers/{SLUG}/uploads/media")
            connection.putheader("X-Ledger-Token", live["admin"])
            connection.putheader("Origin", sorted(server.ALLOWED_ORIGINS)[0])
            connection.putheader("Content-Type", "application/octet-stream")
            connection.putheader("X-Artifact-Name", name)
            if length is not None:
                connection.putheader("Content-Length", length)
            connection.endheaders(b"x" if length == "1" else None)
            response = connection.getresponse()
            assert (response.status, json.loads(response.read())) == (
                400,
                {"error": {"code": "schema_invalid", "message": message}},
            )
        finally:
            connection.close()


def test_item_projection_counts_and_collection_revisions(live):
    from tests.swarm_ledger.test_ledger_authority import core

    core.sync(
        SLUG,
        ops=[
            {
                "op": "add",
                "id": "projection-comment",
                "thread": "phases/p1/comments",
                "by": "operator",
                "text": "Comment",
            },
            {"op": "add_item", "id": "projection-question", "by": "operator", "list": "questions", "text": "Question"},
            {
                "op": "add",
                "id": "projection-answer",
                "by": "operator",
                "thread": "questions/projection-question/answers",
                "text": "Answer",
            },
        ],
    )
    phase = request(live, "GET", "phases/p1")[1]
    question = request(live, "GET", "questions/projection-question")[1]
    assert phase["data"]["comments_count"] == 1
    assert "comments" not in phase["data"]
    assert question["data"]["answers_count"] == 1
    assert question["data"]["comments_count"] == 0
    assert "answers" not in question["data"]
    assert "comments" not in question["data"]
    row = request(live, "GET", "questions")[1]["data"][0]
    assert row["revision"] == question["revision"]
    assert {key: value for key, value in row.items() if key != "revision"} == question["data"]


def test_success_acknowledgment_preserves_bounded_warnings(live, monkeypatch):
    warnings = [f"{index}:" + "w" * 1500 for index in range(22)]
    state = {"tasks": [], "_meta": {"rev": 55, "warnings": warnings}}
    monkeypatch.setattr(server.repository, "apply_ops", lambda slug, **kwargs: (state, []))
    payload = {"ops": [{"op": "sync", "id": "warning-proof"}], "guards": {"metadata": "0" * 64}}
    assert request(live, "POST", "operations", payload) == (
        200,
        {
            "applied": ["warning-proof"],
            "rejected": [],
            "_meta": {"rev": 55, "warnings": [warning[:1000] for warning in warnings[:20]]},
        },
    )


def test_latest_thousand_operation_receipts_remain_retry_safe(live):
    from scripts.swarm_ledger.api.resources import revision

    def operation(index):
        return {"op": "sync", "id": f"receipt-{index}"}

    receipts = {
        str(index): {
            "digest": revision({"ops": [operation(index)], "changes": []}),
            "results": {f"receipt-{index}": True},
        }
        for index in range(1000)
    }

    class SeedReceipts:
        def apply(self, doc, op, ctx, apply_op):
            ctx.meta["api_operations"] = receipts
            ctx.dirty = True
            return apply_op(doc, op, ctx)

    server.repository.apply_ops(SLUG, ops=[{"op": "sync", "id": "seed-receipts"}], gate=SeedReceipts())
    guard = request(live, "GET", "metadata")[1]["revision"]
    payload = {"operation_id": "latest", "ops": [operation("latest")], "guards": {"metadata": guard}}
    assert request(live, "POST", "operations", payload)[0] == 200
    saved = server.repository.get_document(SLUG)["_meta"]["api_operations"]
    assert len(saved) == 1000
    assert "0" not in saved
    assert "1" in saved
    assert "latest" in saved
    for index, status in ((1, 200), (0, 409)):
        retry = {"operation_id": str(index), "ops": [operation(index)], "guards": {"metadata": "0" * 64}}
        assert request(live, "POST", "operations", retry)[0] == status


def test_error_details_keep_empty_defaults_and_exact_byte_limit():
    from scripts.swarm_ledger.api.errors import APIError
    from scripts.swarm_ledger.api.resources import MAX_REPLY

    assert APIError(403, "forbidden", "Refused", {"_meta": {}}).envelope() == {
        "error": {"code": "forbidden", "message": "Refused", "details": {"_meta": {"warnings": []}}}
    }
    assert APIError(403, "forbidden", "Refused", {"padding": "x" * MAX_REPLY}).envelope() == {
        "error": {"code": "forbidden", "message": "Refused", "details": {"rejected": []}}
    }
    base = APIError(403, "forbidden", "Refused", {"rejected": ["a"], "padding": ""}).envelope()
    overhead = len(json.dumps(base, ensure_ascii=False).encode())
    for delta in (-4, 0):
        details = {"rejected": ["a"], "padding": "x" * (MAX_REPLY - overhead + delta)}
        envelope = APIError(403, "forbidden", "Refused", details).envelope()
        assert envelope["error"]["details"] == details
        assert len(json.dumps(envelope, ensure_ascii=False).encode()) == MAX_REPLY + delta
    details = {"rejected": ["a"], "padding": "x" * (MAX_REPLY - overhead + 1)}
    assert APIError(403, "forbidden", "Refused", details).envelope()["error"]["details"] == {"rejected": ["a"]}


def test_missing_ledgers_and_unsupported_operation_methods(live):
    from types import SimpleNamespace

    from scripts.swarm_ledger.api.errors import APIError
    from scripts.swarm_ledger.api.routes import ledger_operation

    for slug in ("absent-ledger", "invalid.slug"):
        status, data, _ = send(live, "GET", f"/api/v1/ledgers/{slug}/metadata", **{"X-Ledger-Token": live["admin"]})
        assert (status, json.loads(data)) == (404, {"error": {"code": "ledger_missing", "message": "No such ledger"}})
    with pytest.raises(APIError) as error:
        ledger_operation(SimpleNamespace(command="DELETE"), server, SLUG, "operations", "")
    assert error.value.status == 405
    assert error.value.envelope() == {"error": {"code": "method_not_allowed", "message": "Use a resource operation"}}


def test_literal_resource_guards_for_threads_members_and_artifacts(live):
    operations = [
        ("members", {"op": "join", "id": "join-guard", "by": "guard-member"}),
        ("chat", {"op": "edit", "id": "seed-chat-one", "thread": "chat", "text": "Edited"}),
        ("chat", {"op": "delete", "id": "seed-chat-one", "thread": "chat"}),
        ("chat", {"op": "clear", "id": "clear-guard", "thread": "chat"}),
        ("artifacts", {"op": "artifact_delete", "id": "artifact-guard", "target": "absent-artifact"}),
    ]
    for path, operation in operations:
        before = request(live, "GET", path)[1]["revision"]
        payload = {"ops": [operation], "guards": {"metadata": request(live, "GET", "metadata")[1]["revision"]}}
        assert request(live, "POST", "operations", payload) == (
            428,
            {"error": {"code": "revision_required", "message": "Every changed resource needs an expected revision"}},
        )
        assert request(live, "GET", path)[1]["revision"] == before
        payload["guards"] = {path: before}
        assert request(live, "POST", "operations", payload)[0] == 200


def test_tick_tasks_span_more_than_one_page(live):
    from scripts.swarm.ledger_client import LedgerClient
    from tests.swarm_ledger.test_ledger_authority import core

    core.sync(
        SLUG,
        ops=[
            {
                "op": "task_add",
                "id": f"task-seed-{index}",
                "by": "swarm",
                "task": f"t{index}",
                "title": f"Task {index}",
                "lane": "eng",
            }
            for index in range(105)
        ],
    )
    rows = LedgerClient(service=True).tasks(SLUG)
    assert [row["id"] for row in rows] == [f"t{index}" for index in range(105)]


def test_service_flag_keeps_operator_and_worker_credentials_distinct(live, monkeypatch):
    import urllib.request
    from types import SimpleNamespace

    from scripts.swarm.ledger_client import LedgerClient
    from tests.swarm_ledger.test_ledger_authority import ledger

    monkeypatch.setattr(ledger.Who, "from_env", lambda: SimpleNamespace(pinned=True, name="api-reader"))
    open_request = urllib.request.urlopen
    callers = []

    def trace(request, **kwargs):
        callers.append(request.get_header("X-ledger-agent"))
        return open_request(request, **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", trace)
    ledger.resource(SLUG, "metadata", service=True)
    assert callers[-1] is None
    ledger.export(SLUG, service=True)
    assert callers[-1] is None
    LedgerClient(service=True).closed(SLUG)
    assert callers[-1] is None
    ledger.resource(SLUG, "metadata")
    assert callers[-1] == "api-reader"
    ledger.export(SLUG)
    assert callers[-1] == "api-reader"
    LedgerClient().closed(SLUG)
    assert callers[-1] == "api-reader"


def test_known_hash_shaped_task_ids_keep_the_comment_exemption(live):
    from tests.swarm_ledger.test_ledger_authority import core

    core.sync(
        SLUG,
        ops=[
            {
                "op": "task_add",
                "id": "hash-task-seed",
                "by": "swarm",
                "task": "deadb33f",
                "title": "Known task",
                "lane": "eng",
            }
        ],
    )
    revision = request(live, "GET", "chat")[1]["revision"]
    payload = {
        "ops": [
            {
                "op": "add",
                "id": "known-task-comment",
                "thread": "chat",
                "by": "api-reader",
                "to": "operator",
                "text": "The deadb33f task is covered",
            }
        ],
        "guards": {"chat": revision},
    }
    status, result = request(live, "POST", "operations", payload)
    assert status == 200
    assert result["applied"] == ["known-task-comment"]
    revision = request(live, "GET", "chat")[1]["revision"]
    payload = {
        "ops": [
            {
                "op": "add",
                "id": "unknown-hash-comment",
                "thread": "chat",
                "by": "api-reader",
                "text": "The feedfac3 task is covered",
            }
        ],
        "guards": {"chat": revision},
    }
    assert request(live, "POST", "operations", payload) == (
        400,
        {
            "error": {
                "code": "schema_invalid",
                "message": "Operation does not match its domain schema: chat refused, write plain words for the "
                "operator (what was done, or why it was skipped): commit hash 'feedfac3'",
            }
        },
    )
    assert request(live, "GET", "chat")[1]["revision"] == revision


def test_refused_comment_reason_reaches_the_api_reply_and_the_cli(live):
    from types import SimpleNamespace
    from unittest.mock import patch

    reason = (
        "Operation does not match its domain schema: comment refused, write plain words for the operator "
        "(what was done, or why it was skipped): file name or path 'foo.py'"
    )
    expected = {"error": {"code": "schema_invalid", "message": reason}}
    thread = "phases/p1/comments"
    before = request(live, "GET", thread)
    comment = {"op": "add", "id": "path-comment", "thread": thread, "by": "api-reader", "text": "see scripts/foo.py"}
    assert request(live, "POST", "operations", {"ops": [comment], "guards": {thread: before[1]["revision"]}}) == (
        400,
        expected,
    )
    with patch.dict("os.environ", {"AGENTIHOOKS_AGENT_NAME": "", "AGENTIHOOKS_SWARM": ""}):
        with pytest.raises(SystemExit) as refused:
            ledger.cmd_comment(
                SimpleNamespace(slug=SLUG, item="phases/p1", text="see scripts/foo.py", name="api-reader")
            )
    prefix = "server refused: 400 "
    assert refused.value.code[: len(prefix)] == prefix
    assert json.loads(refused.value.code[len(prefix) :]) == expected
    assert request(live, "GET", thread) == before


def test_stored_checkbox_receipt_keeps_the_canonical_digest(live):
    from scripts.swarm_ledger.api.resources import revision

    changes = [{"path": "phases/p1/done", "base": False, "value": True}]
    digest = revision({"ops": [{"op": "sync", "id": "stored-checkbox"}], "changes": changes})

    class SeedCheckboxReceipt:
        def apply(self, doc, op, ctx, apply_op):
            ctx.meta["api_operations"] = {"checkbox-request": {"digest": digest, "results": {"stored-checkbox": True}}}
            ctx.dirty = True
            return apply_op(doc, op, ctx)

    server.repository.apply_ops(SLUG, ops=[{"op": "sync", "id": "seed-checkbox"}], gate=SeedCheckboxReceipt())
    before = request(live, "GET", "phases/p1")[1]
    payload = {
        "operation_id": "checkbox-request",
        "id": "stored-checkbox",
        "ops": [],
        "changes": changes,
        "guards": {"phases/p1": "0" * 64},
    }
    status, result = request(live, "POST", "operations", payload)
    assert status == 200
    assert result["applied"] == ["stored-checkbox"]
    assert result["rejected"] == []
    assert request(live, "GET", "phases/p1")[1] == before


def test_cli_collection_consumers_read_all_pages(live, tmp_path, capsys, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from tests.swarm_ledger.test_ledger_authority import core, ledger

    core.sync(
        SLUG,
        ops=[
            {
                "op": "task_add",
                "id": f"cli-task-{index}",
                "by": "swarm",
                "task": f"t{index}",
                "title": f"Task {index}",
                "lane": "eng",
            }
            for index in range(105)
        ],
    )
    core.sync(
        SLUG,
        ops=[
            {
                "op": "phase_add",
                "id": f"cli-phase-{index}",
                "by": "swarm",
                "phase": f"p{index}",
                "title": f"Phase {index}",
            }
            for index in range(2, 106)
        ],
    )
    core.sync(
        SLUG,
        ops=[
            {"op": "add", "id": f"cli-chat-{index}", "thread": "chat", "text": f"Entry {index}"} for index in range(105)
        ],
    )
    ledger.cmd_artifact_purge(SimpleNamespace(slug=SLUG, name="api-reader"))
    assert json.loads(capsys.readouterr().out) == {"purged": 0, "artifacts": 0}
    send_operation = Mock()
    monkeypatch.setattr(ledger, "send", send_operation)
    args = SimpleNamespace(
        action="add",
        id="-",
        slug=SLUG,
        values=["Next"],
        depends_on="",
        territory="",
        overlays="",
        gain=None,
        must="",
        check="",
        judge="",
        push="",
        kind="",
        artifact=False,
        follow_up=False,
        profile="",
        rank="",
        difficulty=None,
        plan="",
        plan_slice="",
        not_duplicate="",
        scaffold=False,
        description="",
        phase="",
        lane="eng",
        name="api-reader",
    )
    ledger.cmd_task(args)
    assert json.loads(capsys.readouterr().out) == {"task": "t105", "added": "Next"}
    assert send_operation.call_args.kwargs["task"] == "t105"
    plan = tmp_path / "phases.json"
    plan.write_text(json.dumps({"phases": [{"title": "Next phase", "description": "Next"}]}))
    ledger.cmd_plan(SimpleNamespace(slug=SLUG, path=str(plan), name="api-reader"))
    assert json.loads(capsys.readouterr().out)["appended"] == ["p106"]
    assert send_operation.call_args.kwargs["phases"][0]["phase"] == "p106"


def test_layout_origin_and_revision_are_exact(live):
    from scripts.swarm_ledger.api.resources import revision

    before = json.loads(send(live, "GET", "/api/v1/layout")[1])
    payload = {"capacity-box": {"height": 350}}
    for headers in ({}, {"Origin": "https://untrusted.example"}):
        status, data, _ = send(
            live,
            "PUT",
            "/api/v1/layout",
            json.dumps(payload).encode(),
            **{"Content-Type": "application/json", **headers},
        )
        assert (status, json.loads(data)) == (
            403,
            {"error": {"code": "forbidden", "message": "Origin not allowed"}},
        )
        assert json.loads(send(live, "GET", "/api/v1/layout")[1]) == before
    status, data, _ = send(
        live,
        "PUT",
        "/api/v1/layout",
        json.dumps(payload).encode(),
        **{"Origin": sorted(server.ALLOWED_ORIGINS)[0], "Content-Type": "application/json"},
    )
    assert status == 200
    result = json.loads(data)
    assert result["data"] == payload
    assert result["revision"] == revision(payload)
    assert result["revision"] != before["revision"]


def test_control_domain_validation_has_an_exact_envelope(live, monkeypatch):
    from unittest.mock import Mock

    control = Mock(side_effect=ValueError("Invalid control"))
    monkeypatch.setattr(server, "swarm_control", control)
    assert request(live, "POST", "swarm/actions", {"action": "pause"}) == (
        400,
        {"error": {"code": "schema_invalid", "message": "Control does not match its domain schema"}},
    )
    control.assert_called_once_with(SLUG, ["pause"], "swarm")


def test_upload_schema_accepts_one_byte_and_optional_name(live, monkeypatch):
    calls = []

    def media(handler, slug):
        calls.append((slug, handler.rfile.read(int(handler.headers["Content-Length"]))))
        handler.send(200, json.dumps({"accepted": True}), "application/json")

    monkeypatch.setattr(server.Handler, "post_media", media)
    headers = {
        "X-Ledger-Token": live["admin"],
        "Origin": sorted(server.ALLOWED_ORIGINS)[0],
        "Content-Type": "application/octet-stream",
    }
    status, data, _ = send(live, "POST", f"/api/v1/ledgers/{SLUG}/uploads/media", b"x", **headers)
    assert (status, json.loads(data)) == (200, {"accepted": True})
    assert calls == [(SLUG, b"x")]
    for content_type in ("text/plain", "image/png"):
        status, data, _ = send(
            live,
            "POST",
            f"/api/v1/ledgers/{SLUG}/uploads/media",
            b"x",
            **{**headers, "Content-Type": content_type},
        )
        assert (status, json.loads(data)) == (
            400,
            {"error": {"code": "schema_invalid", "message": "Request does not match the resource schema"}},
        )
    assert calls == [(SLUG, b"x")]


def test_checkbox_and_domain_operations_keep_order_and_replay(live):
    phase = request(live, "GET", "phases/p1")[1]
    chat = request(live, "GET", "chat")[1]
    payload = {
        "operation_id": "combined-operation",
        "id": "checkbox-first",
        "ops": [{"op": "add", "id": "ordinary-second", "thread": "chat", "text": "Combined"}],
        "changes": [{"path": "phases/p1/done", "base": False, "value": True}],
        "guards": {"phases/p1": phase["revision"], "chat": chat["revision"]},
    }
    first = request(live, "POST", "operations", payload)
    assert first[0] == 200
    assert first[1]["applied"] == ["checkbox-first", "ordinary-second"]
    assert first[1]["rejected"] == []
    assert request(live, "GET", "phases/p1")[1]["data"]["done"] is True
    assert request(live, "GET", "chat")[1]["data"][-1]["text"] == "Combined"
    assert request(live, "POST", "operations", payload) == first


def test_noop_receipt_advances_metadata_once(live):
    before = request(live, "GET", "metadata")[1]["data"]["_meta"]["rev"]
    payload = {
        "operation_id": "noop-receipt",
        "ops": [{"op": "sync", "id": "noop-receipt"}],
        "guards": {"metadata": request(live, "GET", "metadata")[1]["revision"]},
    }
    first = request(live, "POST", "operations", payload)
    assert first[0] == 200
    after = request(live, "GET", "metadata")[1]["data"]["_meta"]["rev"]
    assert after > before
    assert request(live, "POST", "operations", payload) == first
    assert request(live, "GET", "metadata")[1]["data"]["_meta"]["rev"] == after


def test_success_resources_and_acknowledgments_keep_the_exact_byte_boundary():
    from scripts.swarm_ledger.api.errors import APIError
    from scripts.swarm_ledger.api.mutations import bounded_ack
    from scripts.swarm_ledger.api.resources import MAX_REPLY, bounded, page, reply_size

    envelope = {"data": "", "revision": "a" * 64}
    envelope["data"] = "x" * (MAX_REPLY - reply_size(envelope))
    assert reply_size(envelope) == MAX_REPLY
    assert bounded(envelope) is envelope
    overflow = {**envelope, "data": envelope["data"] + "x"}
    with pytest.raises(APIError) as error:
        bounded(overflow)
    assert error.value.status == 413
    assert error.value.envelope() == {
        "error": {"code": "resource_too_large", "message": "Use the explicit export operation for this resource"}
    }
    ack = {"applied": ["a"], "rejected": [], "tasks": [{"id": "t1", "description": ""}]}
    ack["tasks"][0]["description"] = "x" * (MAX_REPLY - reply_size(ack))
    assert reply_size(ack) == MAX_REPLY
    assert bounded_ack(ack) is ack
    assert ack["tasks"][0]["description"]
    assert "task_rows_omitted" not in ack
    with pytest.raises(APIError) as error:
        bounded_ack(overflow)
    assert error.value.status == 413
    first = page([""], "a" * 64, {"limit": 1})
    text = "x" * (MAX_REPLY - reply_size(first))
    result = page([text], "a" * 64, {"limit": 1})
    assert result["data"] == [text]
    assert result["next_cursor"] is None
    assert reply_size(result) == MAX_REPLY
    with pytest.raises(APIError) as error:
        page([text + "x"], "a" * 64, {"limit": 1})
    assert error.value.status == 413
    assert error.value.envelope() == {
        "error": {"code": "resource_too_large", "message": "Use the explicit export operation for this resource"}
    }


def test_relay_requires_its_literal_item_revision(live):
    before = request(live, "GET", "phases/p1")[1]
    payload = {
        "ops": [
            {
                "op": "relay",
                "id": "relay-guard",
                "by": "api-reader",
                "item": "phases/p1",
                "text": "Verified",
                "quote": "Approved",
            }
        ],
        "guards": {"metadata": request(live, "GET", "metadata")[1]["revision"]},
    }
    assert request(live, "POST", "operations", payload) == (
        428,
        {"error": {"code": "revision_required", "message": "Every changed resource needs an expected revision"}},
    )
    assert request(live, "GET", "phases/p1")[1] == before
    payload["guards"] = {"phases/p1": before["revision"]}
    status, reply = request(live, "POST", "operations", payload)
    assert status == 200
    assert reply["applied"] == []
    assert reply["rejected"] == ["relay-guard"]
    assert request(live, "GET", "phases/p1")[1] == before


@pytest.mark.parametrize(
    "path", ["tasks", "sources", "events", "members", "threads", "questions/q1/answers", "questions/q1/comments"]
)
def test_repository_optional_collections_have_empty_http_resources(live, monkeypatch, path):
    from scripts.swarm_ledger.api.resources import revision

    document = {"_meta": {}, "questions": [{"id": "q1"}]}
    monkeypatch.setattr(server.repository, "get_document", lambda slug: document)
    monkeypatch.setattr(server.repository, "read", lambda slug, *keys: document)
    assert request(live, "GET", path) == (
        200,
        {"data": [], "revision": revision([]), "next_cursor": None},
    )


def test_optional_repository_fields_preserve_forbidden_details(live, monkeypatch):
    from tests.swarm_ledger.test_ledger_authority import authority

    document = {"_meta": {}}

    def read(slug):
        assert slug == SLUG
        return document

    monkeypatch.setattr(server.repository, "get_document", read)
    headers = {
        "X-Ledger-Agent": "api-reader",
        "X-Ledger-Token": authority.agent_token(live["admin"], SLUG, "api-reader"),
    }
    payload = {"ops": [{"op": "join", "id": "default-refusal", "by": "other"}], "guards": {}}
    assert request(live, "POST", "operations", payload, **headers) == (
        403,
        {
            "error": {
                "code": "forbidden",
                "message": "Caller cannot perform this operation as its author",
                "details": {
                    "rejected": ["default-refusal"],
                    "_meta": {"warnings": ["api-reader cannot write as other"]},
                },
            }
        },
    )
    assert document == {"_meta": {}}


def test_optional_repository_fields_preserve_success_acknowledgment(live, monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(server.repository, "get_document", lambda slug: {"_meta": {}})
    apply = Mock(return_value=({"_meta": {"rev": 3}}, []))
    monkeypatch.setattr(server.repository, "apply_ops", apply)
    monkeypatch.setattr(server, "relay_to_inbox", Mock())
    monkeypatch.setattr(server, "doctor_phrase", Mock())
    payload = {"ops": [{"op": "sync", "id": "default-success"}], "guards": {}}
    assert request(live, "POST", "operations", payload) == (
        200,
        {"applied": ["default-success"], "rejected": [], "_meta": {"rev": 3, "warnings": []}},
    )
    assert apply.call_args.args == (SLUG,)
    assert apply.call_args.kwargs["ops"] == payload["ops"]


@pytest.mark.parametrize("offset,extra,last", [(7, 0, False), (8, 1, False), (8, 0, True)])
def test_page_byte_budget_preserves_digit_transitions_and_last_page(offset, extra, last):
    from scripts.swarm_ledger.api.resources import MAX_REPLY, page, reply_size

    rev = "a" * 64
    cursor = None if last else f"{rev}:{offset + 2}"
    envelope = {"data": ["", ""], "revision": rev, "next_cursor": cursor}
    text = "x" * (MAX_REPLY - reply_size(envelope) + extra)
    rows = [""] * offset + ["", text] + ([] if last else [""])
    result = page(rows, rev, {"limit": 2, "cursor": f"{rev}:{offset}"})
    selected = [""] if extra else ["", text]
    next_cursor = f"{rev}:{offset + len(selected)}" if offset + len(selected) < len(rows) else None
    assert result == {"data": selected, "revision": rev, "next_cursor": next_cursor}
    assert reply_size(result) <= MAX_REPLY


def test_receipt_for_existing_domain_entry_advances_metadata_once(live):
    metadata = request(live, "GET", "metadata")[1]["data"]["_meta"]["rev"]
    chat = request(live, "GET", "chat")[1]
    payload = {
        "operation_id": "existing-entry-receipt",
        "ops": [{"op": "add", "id": "seed-chat-one", "thread": "chat", "text": "First message"}],
        "guards": {"chat": chat["revision"]},
    }
    first = request(live, "POST", "operations", payload)
    assert first[0] == 200
    assert first[1]["applied"] == ["seed-chat-one"]
    assert request(live, "GET", "chat")[1] == chat
    after = request(live, "GET", "metadata")[1]["data"]["_meta"]["rev"]
    assert after > metadata
    assert request(live, "POST", "operations", payload) == first
    assert request(live, "GET", "metadata")[1]["data"]["_meta"]["rev"] == after


def test_bin_actions_validate_origin_body_and_slug_before_delete(live):
    status, data, _ = send(
        live,
        "POST",
        "/api/v1/bin/actions",
        json.dumps({"action": "delete", "slug": SLUG}).encode(),
        **{"Content-Type": "application/json"},
    )
    assert (status, json.loads(data)) == (
        403,
        {"error": {"code": "forbidden", "message": "Origin not allowed"}},
    )
    for payload in (
        None,
        [],
        "invalid",
        3,
        {"action": "delete", "slug": 3},
        {"action": "delete", "slug": "***"},
    ):
        status, data, _ = send(
            live,
            "POST",
            "/api/v1/bin/actions",
            json.dumps(payload).encode(),
            **{"Content-Type": "application/json", "Origin": sorted(server.ALLOWED_ORIGINS)[0]},
        )
        assert (status, json.loads(data)) == (
            400,
            {"error": {"code": "schema_invalid", "message": "Request does not match the resource schema"}},
        )
        assert request(live, "GET", "metadata")[0] == 200


def test_unsupported_global_action_routes_never_delete(live):
    for method, path in (("PUT", "/api/v1/bin/actions"), ("POST", "/api/v1/absent-route")):
        status, data, _ = send(
            live,
            method,
            path,
            json.dumps({"action": "delete", "slug": SLUG}).encode(),
            **{"Content-Type": "application/json", "Origin": sorted(server.ALLOWED_ORIGINS)[0]},
        )
        assert (status, json.loads(data)) == (
            404,
            {"error": {"code": "resource_missing", "message": "No such resource"}},
        )
        assert request(live, "GET", "metadata")[0] == 200


def test_swarm_read_and_bin_stop_use_the_requested_ledger(live, monkeypatch):
    from unittest.mock import Mock

    status = Mock(return_value={"slug": SLUG, "running": True})
    monkeypatch.setattr(server, "swarm_status", status)
    assert request(live, "GET", "swarm")[0] == 200
    status.assert_called_once_with(SLUG)
    status.reset_mock()
    control = Mock(return_value=({}, None))
    monkeypatch.setattr(server, "swarm_control", control)
    code, data, _ = send(
        live,
        "POST",
        "/api/v1/bin/actions",
        json.dumps({"action": "delete", "slug": SLUG}).encode(),
        **{"Content-Type": "application/json", "Origin": sorted(server.ALLOWED_ORIGINS)[0]},
    )
    assert (code, json.loads(data)) == (200, {"slug": SLUG, "action": "delete"})
    status.assert_called_once_with(SLUG)
    control.assert_called_once_with(SLUG, ["stop", "--now"])


def test_bin_summaries_keep_item_revisions_and_pagination(live, monkeypatch):
    from scripts.swarm_ledger.api.resources import revision

    rows = [{"slug": "one", "title": "First"}, {"slug": "two", "title": "Second"}]
    monkeypatch.setattr(server, "bin_summaries", lambda: rows)
    code, data, _ = send(live, "GET", "/api/v1/bin?limit=1")
    assert (code, json.loads(data)) == (
        200,
        {
            "data": [{**rows[0], "revision": revision(rows[0])}],
            "revision": revision(rows),
            "next_cursor": f"{revision(rows)}:1",
        },
    )
    code, data, _ = send(live, "GET", f"/api/v1/bin?limit=1&cursor={revision(rows)}:1")
    assert (code, json.loads(data)) == (
        200,
        {"data": [{**rows[1], "revision": revision(rows[1])}], "revision": revision(rows), "next_cursor": None},
    )
    code, data, _ = send(live, "GET", "/api/v1/bin/two")
    assert (code, json.loads(data)) == (200, {"data": rows[1], "revision": revision(rows[1])})


def test_oversized_global_item_obeys_the_transport_byte_cap(live, monkeypatch):
    from scripts.swarm_ledger.api.resources import MAX_REPLY

    monkeypatch.setattr(server, "bin_summaries", lambda: [{"slug": "oversized", "title": "x" * MAX_REPLY}])
    code, data, _ = send(live, "GET", "/api/v1/bin/oversized")
    assert (code, json.loads(data)) == (
        413,
        {"error": {"code": "resource_too_large", "message": "Use the explicit export operation for this resource"}},
    )


def test_json_body_without_content_length_has_a_stable_error(live):
    import http.client

    connection = http.client.HTTPConnection("127.0.0.1", live["port"], timeout=5)
    try:
        connection.putrequest("POST", f"/api/v1/ledgers/{SLUG}/operations")
        connection.putheader("X-Ledger-Token", live["admin"])
        connection.putheader("Content-Type", "application/json")
        connection.endheaders()
        response = connection.getresponse()
        assert (response.status, json.loads(response.read())) == (
            400,
            {"error": {"code": "schema_invalid", "message": "Request body must be a bounded JSON object"}},
        )
    finally:
        connection.close()


def test_unicode_resources_preserve_utf8_wire_encoding(live):
    from tests.swarm_ledger.test_ledger_authority import core

    core.sync(SLUG, ops=[{"op": "title_set", "id": "unicode-wire", "text": "Málaga"}])
    code, data, _ = send(live, "GET", f"/api/v1/ledgers/{SLUG}/metadata", **{"X-Ledger-Token": live["admin"]})
    assert code == 200
    assert "Málaga".encode() in data
    assert data == json.dumps(json.loads(data), ensure_ascii=False).encode()


def test_ack_succeeds_while_members_change_between_every_read_and_write(live, capsys):
    from types import SimpleNamespace
    from unittest.mock import patch

    from scripts.swarm_ledger.api.client import ResourceClient
    from tests.swarm_ledger.test_ledger_authority import core, ledger

    read = ResourceClient.request
    joins = iter(range(10))

    def busy(self, slug, path, payload=None):
        reply = read(self, slug, path, payload)
        if path == "members":
            name = f"busy-{next(joins)}"
            core.sync(SLUG, ops=[{"op": "join", "id": name, "by": name}])
        return reply

    with (
        patch.dict("os.environ", {"AGENTIHOOKS_AGENT_NAME": "", "AGENTIHOOKS_SWARM": ""}),
        patch.object(ResourceClient, "request", busy),
    ):
        ledger.cmd_ack(SimpleNamespace(slug=SLUG, name="api-reader", rev=None))
    acked = json.loads(capsys.readouterr().out)["acked"]
    members = server.repository.get_document(SLUG)["_meta"]["members"]
    assert members["api-reader"]["handled_rev"] == acked
    assert "busy-0" in members
