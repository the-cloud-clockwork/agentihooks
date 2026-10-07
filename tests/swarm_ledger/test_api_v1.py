import json

from tests.swarm_ledger.test_ledger_authority import SLUG, send, server

RELAY = server.relay_to_inbox


import pytest

pytestmark = pytest.mark.xdist_group("fakeredis")


from tests.swarm_ledger.test_ledger_authority import live as _authority_live

authority_live = _authority_live


@pytest.fixture
def live(authority_live):
    from tests.swarm_ledger.test_ledger_authority import core, new_ledger

    content = {"title": "Authority", "phases": [{"title": "Proof", "description": "word " * 101}]}
    page = new_ledger.render(new_ledger.build_doc(content), SLUG, authority_live["port"])
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


def request(live, method, resource, body=None, **headers):
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
    assert request(live, "POST", "operations", {"ops": [original], "guards": {"chat": rev}})[0] == 200
    rev = request(live, "GET", "chat")[1]["revision"]
    status, denied = request(
        live,
        "POST",
        "operations",
        {
            "ops": [{**original, "text": "Different content"}],
            "guards": {"chat": rev},
        },
    )
    assert status == 409
    assert denied["error"]["code"] == "operation_conflict"


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
    from tests.swarm_ledger.test_ledger_authority import authority

    headers = {
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
        assert request(live, "POST", "swarm/actions", {"action": "start"}, **headers)[0] == 403
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
    assert request(live, "POST", "operations", payload)[0] == 200
    assert request(live, "GET", path)[1]["data"]["done"] is True
    assert request(live, "POST", "operations", payload)[0] == 200


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
        assert reply["error"]["code"] == "schema_invalid"
    assert request(live, "GET", "metadata") == before


def test_stale_cursor_missing_guard_and_origin_are_rejected(live):
    status, page = request(live, "GET", "chat?limit=1")
    assert status == 200
    payload = {"ops": [{"op": "add", "id": "cursor-change", "thread": "chat", "text": "Cursor changed"}], "guards": {}}
    status, reply = request(live, "POST", "operations", payload)
    assert status == 428
    assert reply["error"]["code"] == "revision_required"
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
