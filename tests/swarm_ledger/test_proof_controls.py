import io
import json
from email.message import Message
from types import SimpleNamespace

import pytest

from scripts.swarm import cli
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm_ledger import ledger_authority as authority
from scripts.swarm_ledger.api import proofs, routes
from tests.swarm_ledger.test_ledger_authority import SLUG, server
from tests.swarm_ledger.test_proof_ledgers import CONTENT
from tests.swarm_ledger.test_proof_ledgers import credential as _credential
from tests.swarm_ledger.test_proof_ledgers import hive as _hive
from tests.swarm_ledger.test_proof_ledgers import live as _live

credential, hive, live = _credential, _hive, _live


@pytest.fixture
def owned(live, credential, hive, monkeypatch):
    store = RedisStore(hive)
    store.create(SwarmConfig(SLUG, "/repo", 1, 0))
    owner = store.next_name(SLUG, "eng")
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "local")
    monkeypatch.setattr(server, "swarm_store", lambda: store)

    def invoke(resource, payload, headers):
        encoded = json.dumps(payload).encode()
        message = Message()
        for key, value in {
            "Host": live["host"],
            "Content-Type": "application/json",
            "Content-Length": str(len(encoded)),
            **headers,
        }.items():
            message[key] = value
        handler = SimpleNamespace(
            command="POST",
            path=f"/api/v1/proof-ledgers{resource}",
            headers=message,
            rfile=io.BytesIO(encoded),
            exists=server.repository.exists,
            send=lambda status, text, kind: (status, json.loads(text)),
        )
        return routes.handle(handler, server.api_services())

    controller = {"X-Controller-Credential": credential}
    status, created = invoke("", {"agent": owner, "task": "contract", "content": CONTENT}, controller)
    assert status == 200
    slug = created["data"]["slug"]
    store.create(SwarmConfig(slug, "/repo", 1, 0, scratch=True))
    parent = {"X-Ledger-Agent": owner, "X-Ledger-Token": authority.agent_token(live["admin"], SLUG, owner)}
    status, issued = invoke(f"/{slug}/credential", {}, parent)
    assert status == 200
    return SimpleNamespace(
        store=store,
        owner=owner,
        slug=slug,
        parent=parent,
        controller=controller,
        proof={"X-Proof-Credential": issued["data"]["credential"]},
        invoke=invoke,
        live=live,
    )


@pytest.mark.parametrize("resource", ["credential", "remove"])
@pytest.mark.parametrize("payload", [None, [], {"unexpected": True}])
def test_proof_identity_operations_require_an_empty_object(owned, resource, payload):
    headers = owned.parent if resource == "credential" else owned.controller
    assert owned.invoke(f"/{owned.slug}/{resource}", payload, headers) == (
        400,
        {"error": {"code": "schema_invalid", "message": "Request does not match the resource schema"}},
    )


@pytest.mark.parametrize("suffix", ["unknown", "remove/extra"])
def test_unknown_proof_routes_do_not_remove_the_fixture(owned, suffix):
    assert owned.invoke(f"/{owned.slug}/{suffix}", {}, owned.controller) == (
        404,
        {"error": {"code": "resource_missing", "message": "No such proof resource"}},
    )
    assert server.repository.exists(owned.slug)


@pytest.mark.parametrize("target", [SLUG, "proof-a1b2c3-missing-1"])
def test_absent_and_real_ledgers_refuse_proof_credentials(owned, target):
    assert owned.invoke(f"/{target}/credential", {}, owned.parent) == (
        403,
        {"error": {"code": "forbidden", "message": "Not an owned proof fixture"}},
    )


@pytest.mark.parametrize("lane", ["master", "ci"])
def test_nonengineer_creation_issues_no_owner_authority(owned, lane):
    name = owned.store.next_name(SLUG, lane)
    status, created = owned.invoke("", {"agent": name, "task": "nonengineer", "content": CONTENT}, owned.controller)
    assert status == 200
    headers = {"X-Ledger-Agent": name, "X-Ledger-Token": authority.agent_token(owned.live["admin"], SLUG, name)}
    assert owned.invoke(f"/{created['data']['slug']}/credential", {}, headers) == (
        403,
        {"error": {"code": "forbidden", "message": "Not an owned proof fixture"}},
    )


def test_missing_proof_refuses_even_its_creating_engineer(owned, monkeypatch):
    exists = server.repository.exists
    monkeypatch.setattr(server.repository, "exists", lambda slug: slug != owned.slug and exists(slug))
    assert owned.invoke(f"/{owned.slug}/credential", {}, owned.parent) == (
        403,
        {"error": {"code": "forbidden", "message": "Not an owned proof fixture"}},
    )


def test_removed_proof_loses_creator_authority_after_restore(owned, monkeypatch):
    monkeypatch.setattr(server, "swarm_status", lambda slug: None)
    status, removed = owned.invoke(f"/{owned.slug}/remove", {}, owned.controller)
    assert status == 200 and removed["data"]["slug"] == owned.slug
    server.repository.restore(owned.slug)
    assert server.repository.exists(owned.slug)
    assert owned.invoke(f"/{owned.slug}/credential", {}, owned.parent) == (
        403,
        {"error": {"code": "forbidden", "message": "Not an owned proof fixture"}},
    )


def test_missing_fixture_page_key_cannot_issue_control_authority(owned, monkeypatch):
    token = server.repository.token
    monkeypatch.setattr(server.repository, "token", lambda slug: None if slug == owned.slug else token(slug))
    assert owned.invoke(f"/{owned.slug}/credential", {}, owned.parent) == (
        403,
        {"error": {"code": "forbidden", "message": "Proof fixture has no credential"}},
    )


def test_only_the_creator_receives_a_named_credential_refusal(owned):
    assert owned.invoke(f"/{owned.slug}/credential", {}, {}) == (
        403,
        {"error": {"code": "forbidden", "message": "Only the creating engineer receives the fixture credential"}},
    )


@pytest.mark.parametrize("headers", [{}, {"X-Proof-Credential": "wrong"}])
def test_bad_fixture_credentials_have_a_named_refusal(owned, headers):
    assert owned.invoke(f"/{owned.slug}/controls", {"control": "pause"}, headers) == (
        403,
        {"error": {"code": "forbidden", "message": "Wrong proof fixture credential"}},
    )


def test_owned_fixture_cannot_control_a_real_swarm_configuration(owned):
    owned.store.update(owned.slug, scratch=False)
    assert owned.invoke(f"/{owned.slug}/controls", {"control": "pause"}, owned.proof) == (
        403,
        {"error": {"code": "forbidden", "message": "Proof controls need a scratch swarm"}},
    )


@pytest.mark.parametrize("action", ["cancel", "attach", "force_stop"])
def test_fixture_controls_refuse_every_ungranted_action(owned, action):
    assert owned.invoke(f"/{owned.slug}/controls", {"control": action}, owned.proof) == (
        403,
        {"error": {"code": "forbidden", "message": "Proof controls permit only pause, drain, set and start"}},
    )


def _queued(owned):
    return [
        json.loads(row)["argv"] for row in owned.store.redis.hgetall(owned.store.key(owned.slug, "commands")).values()
    ]


def test_creator_sets_allowlisted_fields_and_starts_its_own_fixture(owned):
    fields = {"master-agent": "codex", "codex-inbox-transport": "app_server", "max-eng-agents": "2"}
    status, accepted = owned.invoke(f"/{owned.slug}/controls", {"control": "set", "fields": fields}, owned.proof)
    assert status == 200
    assert accepted["data"]["argv"] == [
        "set",
        "codex-inbox-transport=app_server",
        "master-agent=codex",
        "max-eng-agents=2",
    ]
    assert accepted["data"]["command"] == "swarm"
    status, started = owned.invoke(f"/{owned.slug}/controls", {"control": "start"}, owned.proof)
    assert status == 200 and started["data"]["argv"] == ["start"]
    assert sorted(_queued(owned)) == sorted([accepted["data"]["argv"], ["start"]])


@pytest.mark.parametrize(
    "key",
    ["api-url", "autonomy", "masters", "master-per", "compact-limit", "eng-role", "plan-kind", "review-gate", "hive-x"],
)
def test_creator_set_refuses_fields_outside_the_allowlist(owned, key):
    body = {"control": "set", "fields": {key: "1"}}
    assert owned.invoke(f"/{owned.slug}/controls", body, owned.proof) == (
        403,
        {"error": {"code": "forbidden", "message": f"Proof controls cannot set {key}"}},
    )
    assert _queued(owned) == []


@pytest.mark.parametrize(
    "body",
    [
        {"control": "set"},
        {"control": "set", "fields": {}},
        {"control": "set", "fields": []},
        {"control": "set", "fields": {"master-agent": 1}},
        {"control": "set", "fields": {"master-agent": ""}},
        {"control": "set", "fields": {"master-agent": "codex extra"}},
        {"control": "set", "fields": {"master-agent": "codex=claude"}},
        {"control": "set", "fields": {"master-agent": "-codex"}},
        {"control": "set", "fields": {"master-agent": "codex\n"}},
        {"control": "set", "fields": {"master-agent": "c" * 65}},
        {"control": "set", "fields": {"master-agent": "codex"}, "extra": True},
        {"control": "start", "extra": True},
    ],
)
def test_creator_set_and_start_refuse_malformed_requests(owned, body):
    assert owned.invoke(f"/{owned.slug}/controls", body, owned.proof) == (
        400,
        {"error": {"code": "schema_invalid", "message": "Request does not match the resource schema"}},
    )
    assert _queued(owned) == []


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("codex-inbox-transport", "legacy"),
        ("codex-inbox-transport", "app_server"),
        ("max-eng-agents", "0"),
        ("max-ci-agents", "100"),
        ("max-plan-agents", "7"),
        ("max-ci-agents", "9"),
        ("eng-model", "a0.9"),
        ("eng-agent", "auto"),
        ("ci-agent", "claude"),
        ("plan-agent", "codex"),
        ("master-agent", "codex"),
        *(
            (f"{lane}-{field}", "Ab.1:x-z")
            for lane in ("eng", "ci", "plan", "master")
            for field in ("model", "effort", "profile")
        ),
        ("eng-model", "x"),
    ],
)
def test_creator_sets_each_allowlisted_field(owned, key, value):
    status, accepted = owned.invoke(f"/{owned.slug}/controls", {"control": "set", "fields": {key: value}}, owned.proof)
    assert status == 200 and accepted["data"]["argv"] == ["set", f"{key}={value}"]


@pytest.mark.parametrize(
    "fields",
    [
        {"codex-inbox-transport": "other"},
        {"max-eng-agents": "two"},
        {"max-ci-agents": "1000"},
        {"max-plan-agents": "-1"},
        {"max-eng-agents": "1\n"},
        {"max-ci-agents": ""},
        {"max-plan-agents": "1000"},
        *({f"{lane}-agent": "gemini"} for lane in ("eng", "ci", "plan", "master")),
    ],
)
def test_creator_set_refuses_values_the_field_cannot_take(owned, fields):
    assert owned.invoke(f"/{owned.slug}/controls", {"control": "set", "fields": fields}, owned.proof) == (
        400,
        {"error": {"code": "schema_invalid", "message": "Request does not match the resource schema"}},
    )
    assert _queued(owned) == []


def test_creator_set_names_the_first_refused_field_in_sorted_order(owned):
    body = {"control": "set", "fields": {"zeta-field": "1", "api-url": "1"}}
    assert owned.invoke(f"/{owned.slug}/controls", body, owned.proof) == (
        403,
        {"error": {"code": "forbidden", "message": "Proof controls cannot set api-url"}},
    )


def test_creator_set_accepts_the_longest_plain_value(owned):
    value = "c" * 64
    status, accepted = owned.invoke(
        f"/{owned.slug}/controls", {"control": "set", "fields": {"eng-model": value}}, owned.proof
    )
    assert status == 200 and accepted["data"]["argv"] == ["set", f"eng-model={value}"]


@pytest.mark.parametrize("body", [{"control": "set", "fields": {"master-agent": "codex"}}, {"control": "start"}])
def test_set_and_start_refuse_a_real_swarm(owned, body):
    owned.store.update(owned.slug, scratch=False)
    assert owned.invoke(f"/{owned.slug}/controls", body, owned.proof) == (
        403,
        {"error": {"code": "forbidden", "message": "Proof controls need a scratch swarm"}},
    )
    assert _queued(owned) == []


def test_a_non_creating_agent_cannot_set_or_start_the_fixture(owned):
    other = owned.store.next_name(SLUG, "eng")
    headers = {"X-Ledger-Agent": other, "X-Ledger-Token": authority.agent_token(owned.live["admin"], SLUG, other)}
    assert owned.invoke(f"/{owned.slug}/credential", {}, headers) == (
        403,
        {"error": {"code": "forbidden", "message": "Only the creating engineer receives the fixture credential"}},
    )
    status, created = owned.invoke("", {"agent": other, "task": "other", "content": CONTENT}, owned.controller)
    assert status == 200
    theirs = created["data"]["slug"]
    owned.store.create(SwarmConfig(theirs, "/repo", 1, 0, scratch=True))
    status, issued = owned.invoke(f"/{theirs}/credential", {}, headers)
    assert status == 200
    for body in ({"control": "set", "fields": {"master-agent": "codex"}}, {"control": "start"}):
        assert owned.invoke(f"/{owned.slug}/controls", body, {"X-Proof-Credential": issued["data"]["credential"]}) == (
            403,
            {"error": {"code": "forbidden", "message": "Wrong proof fixture credential"}},
        )
        assert owned.invoke(f"/{theirs}/controls", body, owned.proof) == (
            403,
            {"error": {"code": "forbidden", "message": "Wrong proof fixture credential"}},
        )
    assert _queued(owned) == []


def test_stale_drain_refusal_keeps_its_protocol_status_and_reason(owned):
    body = {
        "control": "drain",
        "agent": owned.owner,
        "op": "stale-drain",
        "incarnation": "stale",
        "generation": 1,
        "task_revision": 0,
    }
    assert owned.invoke(f"/{owned.slug}/controls", body, owned.proof) == (
        409,
        {
            "error": {
                "code": "control_refused",
                "message": "the page is stale: this agent was replaced, finished or moved task; wait for the next swarm view",
            }
        },
    )


def test_drain_service_refusal_keeps_its_protocol_status_and_reason(owned, monkeypatch):
    monkeypatch.setattr(server, "lifecycle_control", lambda slug, payload, operator: (None, "fixture unavailable"))
    assert owned.invoke(f"/{owned.slug}/controls", {"control": "drain"}, owned.proof) == (
        409,
        {"error": {"code": "control_refused", "message": "fixture unavailable"}},
    )


def test_existing_fixture_credential_remains_valid_for_its_original_scope(owned, monkeypatch):
    status, created = owned.invoke("", {"agent": owned.owner, "task": "vector", "content": CONTENT}, owned.controller)
    assert status == 200
    slug = created["data"]["slug"]
    owned.store.create(SwarmConfig(slug, "/repo", 1, 0, scratch=True))
    token = server.repository.token
    monkeypatch.setattr(server.repository, "token", lambda target: "fixture-page" if target == slug else token(target))
    assert (slug, owned.owner) == ("proof-a1b2c3-vector-1", "engineer@a1b2c3-0001")
    reference = "58f200d189280e75fc009eb29fdf02b9ccdd0ed9717c2dd70d1c907c9b86403d"
    status, issued = owned.invoke(f"/{slug}/credential", {}, owned.parent)
    assert status == 200 and issued["data"]["credential"] == reference
    status, accepted = owned.invoke(f"/{slug}/controls", {"control": "pause"}, {"X-Proof-Credential": reference})
    assert status == 200 and accepted["data"]["argv"] == ["pause"]


def test_proof_control_requires_its_request_file(capsys):
    with pytest.raises(SystemExit) as refused:
        cli.build_parser().parse_args(["proof-a1b2c3-contract-1", "proof-control"])
    assert refused.value.code == 2
    assert "--request" in capsys.readouterr().err


def test_controller_migrates_missing_external_creator_once(owned):
    key = owned.store.key(owned.slug, "proof-creator")
    owned.store.redis.delete(key)
    payload = {"parent": "external-parent", "agent": owned.owner, "task": "contract"}
    for _ in range(2):
        status, result = owned.invoke(f"/{owned.slug}/migrate", payload, owned.controller)
        assert status == 200
        assert result == {"data": {"parent": "external-parent", "owner": owned.owner}}
    assert owned.store.redis.hgetall(key) == {"parent": "external-parent", "owner": owned.owner}


def test_migration_requires_controller_and_preserves_existing_creator(owned):
    payload = {"parent": "external-parent", "agent": owned.owner, "task": "contract"}
    before = owned.store.redis.hgetall(owned.store.key(owned.slug, "proof-creator"))
    status, _ = owned.invoke(f"/{owned.slug}/migrate", payload, owned.parent)
    assert status == 403
    status, result = owned.invoke(f"/{owned.slug}/migrate", payload, owned.controller)
    assert status == 409
    assert result["error"] == {
        "code": "ownership_conflict",
        "message": "Proof creator already differs from the requested migration",
    }
    assert owned.store.redis.hgetall(owned.store.key(owned.slug, "proof-creator")) == before


@pytest.mark.parametrize(
    "field,value",
    [("agent", "engineer@fedcba-0001"), ("agent", "master@a1b2c3-0001"), ("agent", "invalid"), ("task", "another")],
)
def test_migration_refuses_a_different_proof_identity(owned, field, value):
    owned.store.redis.delete(owned.store.key(owned.slug, "proof-creator"))
    payload = {"parent": "external-parent", "agent": owned.owner, "task": "contract", field: value}
    status, result = owned.invoke(f"/{owned.slug}/migrate", payload, owned.controller)
    assert status == 403
    assert result["error"] == {"code": "forbidden", "message": "Migration must match the proof engineer and task"}
    assert owned.store.redis.hgetall(owned.store.key(owned.slug, "proof-creator")) == {}


def test_controller_controls_use_the_existing_drain_fences(owned, monkeypatch):
    request = {
        "control": "drain",
        "agent": "attempt",
        "op": "chosen",
        "incarnation": "old",
        "generation": 3,
        "task_revision": 4,
    }
    seen = []
    monkeypatch.setattr(
        server,
        "lifecycle_control",
        lambda slug, body, operator: (seen.append((slug, body, operator)) or None, "stale attempt"),
    )
    payload = {"owner": {"parent": SLUG, "agent": owned.owner, "task": "contract"}, "request": request}
    status, result = owned.invoke(f"/{owned.slug}/controller-controls", payload, owned.controller)
    assert status == 409
    assert result["error"]["message"] == "stale attempt"
    assert seen == [(owned.slug, request, False)]


def test_controller_controls_refuse_wrong_owner_and_ungranted_actions(owned):
    payload = {
        "owner": {"parent": "wrong-parent", "agent": owned.owner, "task": "contract"},
        "request": {"control": "pause"},
    }
    status, result = owned.invoke(f"/{owned.slug}/controller-controls", payload, owned.controller)
    assert status == 409
    assert result["error"] == {
        "code": "ownership_conflict",
        "message": "Proof creator differs from the configured requester",
    }
    payload["owner"]["parent"] = SLUG
    payload["request"] = {"control": "resume"}
    status, result = owned.invoke(f"/{owned.slug}/controller-controls", payload, owned.controller)
    assert status == 403
    assert result["error"]["message"] == "Proof controls permit only pause, drain, set and start"


def test_controller_controls_do_not_enqueue_pause_again(owned):
    owned.store.update(owned.slug, state="paused")
    payload = {"owner": {"parent": SLUG, "agent": owned.owner, "task": "contract"}, "request": {"control": "pause"}}
    status, result = owned.invoke(f"/{owned.slug}/controller-controls", payload, owned.controller)
    assert status == 200
    assert result == {"data": {"state": "paused"}}


@pytest.mark.parametrize("target", ["missing", "binned", "real", "nonscratch"])
def test_migration_refuses_missing_real_and_nonscratch_targets(owned, target, monkeypatch):
    slug = owned.slug
    if target == "missing":
        exists = server.repository.exists
        monkeypatch.setattr(server.repository, "exists", lambda other: other != slug and exists(other))
    elif target == "binned":
        server.repository.delete(slug)
    elif target == "real":
        slug = SLUG
    else:
        owned.store.update(slug, scratch=False)
    payload = {"parent": "external-parent", "agent": owned.owner, "task": "contract"}
    status, result = owned.invoke(f"/{slug}/migrate", payload, owned.controller)
    assert status == 403
    assert result["error"] == {"code": "forbidden", "message": "Migration needs an existing scratch proof"}


def test_migration_fences_a_concurrent_owner(owned, monkeypatch):
    key = owned.store.key(owned.slug, "proof-creator")
    owned.store.redis.delete(key)
    pipeline = owned.store.redis.pipeline
    other = {"parent": "racing-parent", "owner": owned.owner}

    def racing_pipeline():
        pipe = pipeline()
        execute = pipe.execute

        def race():
            owned.store.redis.hset(key, mapping=other)
            return execute()

        pipe.execute = race
        return pipe

    monkeypatch.setattr(owned.store.redis, "pipeline", racing_pipeline)
    payload = {"parent": "external-parent", "agent": owned.owner, "task": "contract"}
    status, result = owned.invoke(f"/{owned.slug}/migrate", payload, owned.controller)
    assert status == 409
    assert result["error"] == {"code": "ownership_conflict", "message": "Proof creator changed during migration"}
    assert owned.store.redis.hgetall(key) == other


def test_controller_controls_require_controller_and_enqueue_only_scoped_pause(owned):
    payload = {"owner": {"parent": SLUG, "agent": owned.owner, "task": "contract"}, "request": {"control": "pause"}}
    status, result = owned.invoke(f"/{owned.slug}/controller-controls", payload, owned.parent)
    assert status == 403
    assert result["error"]["message"] == "Proof ledgers need the controller credential"
    status, result = owned.invoke(f"/{owned.slug}/controller-controls", payload, owned.controller)
    assert status == 200
    assert result["data"]["argv"] == ["pause"]
    assert result["data"]["state"] == "pending"
    assert "credential" not in result["data"]


@pytest.mark.parametrize("owner", [{}, {"parent": "", "agent": "engineer@a1b2c3-0001", "task": "contract"}])
def test_migration_refuses_incomplete_requester_identity(owned, owner):
    with pytest.raises(proofs.APIError) as refused:
        proofs.migrate(server.api_services(), owned.slug, owner)
    assert refused.value.status == 400
    assert owned.store.redis.hgetall(owned.store.key(owned.slug, "proof-creator")) == {
        "parent": SLUG,
        "owner": owned.owner,
    }


@pytest.mark.parametrize(
    "change", ["parent", "agent", "task", "extra", "parent-type", "agent-type", "task-type", "task-pattern", "list"]
)
def test_migration_schema_refuses_malformed_payload_before_authority(owned, change):
    payload = {"parent": "external-parent", "agent": owned.owner, "task": "contract"}
    if change in ("parent", "agent", "task"):
        payload.pop(change)
    elif change == "extra":
        payload["extra"] = True
    elif change.endswith("-type"):
        payload[change.removesuffix("-type")] = 1
    elif change == "task-pattern":
        payload["task"] = "Contract"
    else:
        payload = []
    with pytest.raises(proofs.APIError) as refused:
        proofs.migrate(server.api_services(), owned.slug, payload)
    assert refused.value.status == 400
    assert refused.value.code == "schema_invalid"


@pytest.mark.parametrize("change", ["owner", "request", "extra", "request-type", "owner-type", "list"])
def test_controller_control_schema_refuses_malformed_payload(owned, change):
    payload = {"owner": {"parent": SLUG, "agent": owned.owner, "task": "contract"}, "request": {"control": "pause"}}
    if change in ("owner", "request"):
        payload.pop(change)
    elif change == "extra":
        payload["extra"] = True
    elif change.endswith("-type"):
        payload[change.removesuffix("-type")] = []
    else:
        payload = []
    with pytest.raises(proofs.APIError) as refused:
        proofs.controller_control(server.api_services(), owned.slug, payload)
    assert refused.value.status == 400
    assert refused.value.code == "schema_invalid"
