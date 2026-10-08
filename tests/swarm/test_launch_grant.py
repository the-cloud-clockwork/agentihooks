import base64
import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError
from scripts.swarm_v2.auth_context import (
    MAX_TTL_SECONDS,
    GrantRefused,
    LaunchAuthority,
    LaunchKey,
    launch_grant_rejections,
    launch_grant_rejections_total,
)

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures/swarm_v2/launch-grant.json").read_text())
SCHEMA = json.loads((Path(__file__).parents[2] / "docs/swarm-v2/schemas/launch-grant.json").read_text())
KEY = LaunchKey(FIXTURE["key_id"], hashlib.sha256(FIXTURE["key_label"].encode()).digest())
PROJECTS = FIXTURE["grant"]["project_ids"]
ISSUED_AT = "2026-10-08T09:00:00Z"


class Clock:
    def __init__(self, now=FIXTURE["now"]):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def store():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(FIXTURE["swarm"], "agentihooks", 1, 0))
    return store


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def authority(store, clock):
    return LaunchAuthority(store, KEY, FIXTURE["issuer"], FIXTURE["audience"], clock, FIXTURE["ttl"])


@pytest.fixture
def execution(store):
    return admit(store, "eng-1@fixture")


def admit(store, seat, previous=""):
    name = store.next_name("fixture", "eng") if not previous else store.execution("fixture", previous).name
    agent = AgentRecord(name, "eng", FIXTURE["task"], seat=seat, started_at=123)
    return store.start_execution("fixture", agent, previous)


def issue(authority, execution, **changes):
    claims = {**FIXTURE["grant"], **changes}
    return authority.issue("fixture", execution.execution_id, **claims)


def body(execution, **changes):
    return {
        "swarm_id": "fixture",
        "execution_id": execution.execution_id,
        "generation": execution.generation,
        "seat_id": execution.seat,
        "task_id": execution.task,
        "account": FIXTURE["grant"]["account"],
        "brain_id": FIXTURE["grant"]["brain_id"],
        "project_ids": list(PROJECTS),
        **changes,
    }


def protected(store):
    return {
        key: store.redis.dump(key) for key in store.redis.scan_iter() if not key.endswith("launch-grant-rejections")
    }


def claims_of(token):
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def signed(payload):
    import hmac

    mac = hmac.new(KEY.secret, f"v2.{payload}".encode(), hashlib.sha256).digest()
    return f"v2.{payload}.{base64.urlsafe_b64encode(mac).rstrip(b'=').decode()}"


def encoded(value):
    return (
        base64.urlsafe_b64encode(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
        .rstrip(b"=")
        .decode()
    )


def resign(token, **changes):
    return signed(encoded({**claims_of(token), **changes}))


@pytest.mark.parametrize("repeat", range(2))
def test_a_launched_worker_registers_only_its_own_execution_and_corpus(store, authority, execution, repeat):
    token = issue(authority, execution)
    registration = authority.register("fixture", token, body(execution))
    assert registration.execution_id == execution.execution_id
    assert registration.generation == execution.generation == 1
    assert registration.seat_id == "eng-1@fixture"
    assert registration.task_id == "task"
    assert registration.project_ids == PROJECTS
    assert registration.brain_id == "swarm-brain"
    assert registration.account == "fixture-account"
    assert registration.issuer == "controller@fixture"
    assert registration.audience == "registry@fixture"
    assert registration.key_id == "fixture-key-1"
    assert registration.registered_at == ISSUED_AT
    assert registration.grant_id == claims_of(token)["grant_id"]
    assert authority.registration("fixture", execution.execution_id) == registration
    assert registration.session_grant().admits(PROJECTS[0])
    assert not registration.session_grant().admits("github.com/the-cloud-clockwork/antoncore")
    assert store.execution("fixture", execution.execution_id) == execution
    assert launch_grant_rejections_total(store, "fixture") == 0


def test_the_grant_subject_comes_from_admitted_state(store, authority, execution):
    claims = claims_of(issue(authority, execution))
    assert claims == {
        "schema_version": "2.0",
        "grant_id": claims["grant_id"],
        "issuer": "controller@fixture",
        "audience": "registry@fixture",
        "key_id": "fixture-key-1",
        "swarm_id": "fixture",
        "execution_id": execution.execution_id,
        "generation": 1,
        "seat_id": "eng-1@fixture",
        "task_id": "task",
        "account": "fixture-account",
        "brain_id": "swarm-brain",
        "project_ids": PROJECTS,
        "issued_at": ISSUED_AT,
        "expires_at": "2026-10-08T09:05:00Z",
    }
    assert claims["grant_id"].startswith("lgr-")
    with pytest.raises(GrantRefused) as error:
        issue(authority, admit(store, "eng-1@fixture", execution.execution_id), project_ids=[])
    assert str(error.value) == "a launch grant needs at least one project"


def test_issued_grants_are_audited_without_secret_material(store, authority, execution):
    token = issue(authority, execution)
    rows = store.redis.hgetall(store.key("fixture", "launch-grants"))
    grant_id = claims_of(token)["grant_id"]
    assert list(rows) == [grant_id]
    assert json.loads(rows[grant_id]) == {
        "grant_id": grant_id,
        "issuer": "controller@fixture",
        "audience": "registry@fixture",
        "key_id": "fixture-key-1",
        "execution_id": execution.execution_id,
        "generation": 1,
        "issued_at": ISSUED_AT,
        "expires_at": "2026-10-08T09:05:00Z",
        "state": "issued",
    }
    dumped = json.dumps({key: store.redis.dump(key).hex() for key in store.redis.scan_iter()})
    for part in token.split(".")[1:]:
        assert part not in dumped
    assert KEY.secret.hex() not in dumped
    authority.register("fixture", token, body(execution))
    assert json.loads(store.redis.hget(store.key("fixture", "launch-grants"), grant_id))["state"] == "registered"


def test_a_replaced_execution_cannot_be_issued_a_grant(store, authority, execution):
    admit(store, "eng-1@fixture", execution.execution_id)
    with pytest.raises(GrantRefused) as error:
        issue(authority, execution)
    assert error.value.error_class == "stale_generation"
    assert str(error.value) == "execution is not the current attempt of its seat"
    with pytest.raises(SwarmError):
        authority.issue("fixture", "exe-unknown", **FIXTURE["grant"])


@pytest.mark.parametrize(
    "case, error_class, message",
    [
        ("expired", "unauthenticated", "launch grant has expired"),
        ("not_yet_valid", "unauthenticated", "launch grant is not yet valid"),
        ("wrong_audience", "unauthenticated", "launch grant is for another audience"),
        ("wrong_issuer", "unauthenticated", "launch grant is from another issuer"),
        ("wrong_key", "unauthenticated", "launch grant signature is invalid"),
        ("other_key_id", "unauthenticated", "launch grant names another signing key"),
        ("tampered", "unauthenticated", "launch grant signature is invalid"),
        ("malformed", "unauthenticated", "launch grant is malformed"),
        ("not_base64", "unauthenticated", "launch grant is malformed"),
        ("not_json", "unauthenticated", "launch grant is malformed"),
        ("list_payload", "unauthenticated", "launch grant is malformed"),
        ("missing_claim", "unauthenticated", "launch grant is malformed"),
        ("extra_claim", "unauthenticated", "launch grant is malformed"),
        ("not_text", "unauthenticated", "launch grant is malformed"),
        ("other_prefix", "unauthenticated", "launch grant is malformed"),
        ("version", "invalid_request", "unsupported launch grant version"),
        ("not_issued", "unauthenticated", "launch grant was not issued by this controller"),
        ("other_swarm", "forbidden_scope", "launch grant belongs to another swarm"),
        ("mismatched_generation", "forbidden_scope", "registration generation is outside the launch grant"),
        ("replaced_execution", "stale_generation", "launch grant is for a superseded execution"),
        ("altered_project_body", "forbidden_scope", "registration project_ids is outside the launch grant"),
        ("other_task", "forbidden_scope", "registration task_id is outside the launch grant"),
        ("other_brain", "forbidden_scope", "registration brain_id is outside the launch grant"),
        ("other_account", "forbidden_scope", "registration account is outside the launch grant"),
        ("other_execution", "forbidden_scope", "registration execution_id is outside the launch grant"),
        ("other_seat", "forbidden_scope", "registration seat_id is outside the launch grant"),
        ("body_swarm", "forbidden_scope", "registration swarm_id is outside the launch grant"),
        ("unknown_field", "invalid_request", "registration names an unsupported identity field"),
        ("missing_execution", "invalid_request", "registration must name its execution and generation"),
        ("not_mapping", "invalid_request", "registration body must be an object"),
    ],
)
def test_a_bad_grant_or_body_fails_before_any_registry_write(
    store, authority, clock, execution, case, error_class, message
):
    token = issue(authority, execution)
    request = body(execution)
    verifier = authority
    slug = "fixture"
    if case == "expired":
        clock.now += FIXTURE["cases"]["expired"]["advance"]
    elif case == "not_yet_valid":
        clock.now -= 1
    elif case == "wrong_audience":
        verifier = LaunchAuthority(store, KEY, FIXTURE["issuer"], FIXTURE["cases"]["wrong_audience"]["audience"], clock)
    elif case == "wrong_issuer":
        verifier = LaunchAuthority(store, KEY, "controller@other", FIXTURE["audience"], clock)
    elif case == "wrong_key":
        verifier = LaunchAuthority(
            store, LaunchKey("fixture-key-1", bytes(32)), FIXTURE["issuer"], FIXTURE["audience"], clock
        )
    elif case == "other_key_id":
        token = resign(token, key_id="fixture-key-2")
    elif case == "tampered":
        token = token.replace(
            token.split(".")[1],
            base64.urlsafe_b64encode(json.dumps({**claims_of(token), "task_id": "other"}).encode())
            .rstrip(b"=")
            .decode(),
        )
    elif case == "malformed":
        token = token.rsplit(".", 1)[0]
    elif case == "not_base64":
        token = signed("!!!!")
    elif case == "not_json":
        token = signed(base64.urlsafe_b64encode(b"{grant").rstrip(b"=").decode())
    elif case == "list_payload":
        token = signed(encoded([claims_of(token)]))
    elif case == "missing_claim":
        token = signed(encoded({k: v for k, v in claims_of(token).items() if k != "key_id"}))
    elif case == "extra_claim":
        token = resign(token, scope="fleet")
    elif case == "not_text":
        token = None
    elif case == "other_prefix":
        token = "v3." + token.split(".", 1)[1]
    elif case == "version":
        token = resign(token, schema_version="3.0")
    elif case == "not_issued":
        token = resign(token, grant_id="lgr-forged")
    elif case == "other_swarm":
        store.create(SwarmConfig("other", "agentihooks", 1, 0))
        slug = "other"
    elif case == "mismatched_generation":
        request = body(execution, generation=2)
    elif case == "replaced_execution":
        admit(store, "eng-1@fixture", execution.execution_id)
    elif case == "altered_project_body":
        request = body(execution, **FIXTURE["cases"]["altered_project_body"]["body"])
    elif case == "other_task":
        request = body(execution, task_id="other")
    elif case == "other_brain":
        request = body(execution, brain_id="personal-brain")
    elif case == "other_account":
        request = body(execution, account="other-account")
    elif case == "other_execution":
        request = body(execution, execution_id="exe-other")
    elif case == "other_seat":
        request = body(execution, seat_id="eng-2@fixture")
    elif case == "body_swarm":
        request = body(execution, swarm_id="other")
    elif case == "unknown_field":
        request = body(execution, controller_epoch=4)
    elif case == "missing_execution":
        request.pop("generation")
    elif case == "not_mapping":
        request = ["fixture"]
    before = protected(store)
    with pytest.raises(GrantRefused) as error:
        verifier.register(slug, token, request)
    assert (error.value.error_class, str(error.value)) == (error_class, message)
    assert protected(store) == before
    assert launch_grant_rejections(store, slug) == {error_class: 1}
    assert launch_grant_rejections_total(store, slug) == 1
    for part in (token or "").split("."):
        assert len(part) < 3 or part not in str(error.value)
    assert not store.redis.hgetall(store.key("fixture", "launch-registrations"))


def test_a_grant_is_valid_until_its_last_second(store, authority, clock, execution):
    token = issue(authority, execution)
    clock.now += FIXTURE["ttl"] - 1
    assert authority.register("fixture", token, body(execution)).execution_id == execution.execution_id


def test_a_retried_registration_returns_the_original_identity(store, authority, clock, execution):
    token = issue(authority, execution)
    first = authority.register("fixture", token, body(execution))
    clock.now += 60
    restarted = LaunchAuthority(store, KEY, FIXTURE["issuer"], FIXTURE["audience"], clock, FIXTURE["ttl"])
    before = protected(store)
    assert restarted.register("fixture", token, body(execution, project_ids=list(reversed(PROJECTS)))) == first
    assert protected(store) == before
    assert first.registered_at == ISSUED_AT
    assert len(store.executions("fixture", "eng-1@fixture")) == 1
    assert len(store.agents("fixture")) == 1


def test_a_second_grant_cannot_reregister_an_execution(store, authority, execution):
    authority.register("fixture", issue(authority, execution), body(execution))
    second = issue(authority, execution)
    before = protected(store)
    with pytest.raises(GrantRefused) as error:
        authority.register("fixture", second, body(execution))
    assert (error.value.error_class, str(error.value)) == (
        "forbidden_scope",
        "execution is already registered under another launch grant",
    )
    assert protected(store) == before


def test_a_stale_attempt_cannot_overwrite_a_newer_registration(store, authority, execution):
    old = issue(authority, execution)
    authority.register("fixture", old, body(execution))
    newer = admit(store, "eng-1@fixture", execution.execution_id)
    current = authority.register("fixture", issue(authority, newer), body(newer))
    before = protected(store)
    with pytest.raises(GrantRefused) as error:
        authority.register("fixture", old, body(execution))
    assert error.value.error_class == "stale_generation"
    assert protected(store) == before
    assert authority.registration("fixture", newer.execution_id) == current
    assert authority.registration("fixture", execution.execution_id).generation == 1


def test_rollback_revokes_outstanding_grants_and_keeps_registrations(store, authority, execution):
    registered = authority.register("fixture", issue(authority, execution), body(execution))
    other = admit(store, "eng-2@fixture")
    outstanding = issue(authority, other)
    assert authority.revoke_outstanding("fixture") == [claims_of(outstanding)["grant_id"]]
    assert authority.revoke_outstanding("fixture") == []
    with pytest.raises(GrantRefused) as error:
        authority.register("fixture", outstanding, body(other))
    assert (error.value.error_class, str(error.value)) == ("unauthenticated", "launch grant was revoked")
    assert authority.registration("fixture", execution.execution_id) == registered
    assert authority.registration("fixture", other.execution_id) is None


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"project_ids": ["unknown"]}, "launch grant project is not a canonical project ID"),
        ({"project_ids": ["agentihooks"]}, "launch grant project is not a canonical project ID"),
        ({"project_ids": [7]}, "launch grant project is not a canonical project ID"),
        ({"brain_id": ""}, "launch grant brain is not an identifier"),
        ({"brain_id": "brain id"}, "launch grant brain is not an identifier"),
        ({"account": "a/b"}, "launch grant account is not an identifier"),
        ({"account": 1}, "launch grant account is not an identifier"),
    ],
)
def test_invalid_issue_requests_write_nothing(store, authority, execution, changes, message):
    before = protected(store)
    with pytest.raises(GrantRefused) as error:
        issue(authority, execution, **changes)
    assert (error.value.error_class, str(error.value)) == ("invalid_request", message)
    assert protected(store) == before


def test_project_ids_are_sorted_and_unique(store, authority, execution):
    projects = ["local:b", "github.com/o/a", "local:b"]
    assert claims_of(issue(authority, execution, project_ids=projects))["project_ids"] == ["github.com/o/a", "local:b"]


def test_signing_keys_and_lifetimes_are_bounded(store, clock):
    with pytest.raises(ValueError, match="at least 32 bytes"):
        LaunchKey("fixture-key-1", bytes(31))
    with pytest.raises(ValueError, match="key ID"):
        LaunchKey("key id", bytes(32))
    assert repr(KEY.secret) not in repr(KEY)
    assert "fixture-key-1" in repr(KEY)
    for ttl in (0, MAX_TTL_SECONDS + 1):
        with pytest.raises(ValueError, match="lifetime"):
            LaunchAuthority(store, KEY, FIXTURE["issuer"], FIXTURE["audience"], clock, ttl)
    assert LaunchAuthority(store, KEY, FIXTURE["issuer"], FIXTURE["audience"], clock, MAX_TTL_SECONDS).ttl == 900
    assert LaunchAuthority(store, KEY, FIXTURE["issuer"], FIXTURE["audience"], clock).ttl == 300


def test_grant_and_registration_match_the_schema(store, authority, execution):
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    common = json.loads((Path(__file__).parents[2] / "docs/swarm-v2/schemas/common.json").read_text())
    registry = Registry().with_resource(common["$id"], Resource.from_contents(common))
    token = issue(authority, execution)
    claims = Draft202012Validator(
        {"$ref": "urn:swarm-v2:launch-grant#/$defs/claims"},
        registry=registry.with_resource(SCHEMA["$id"], Resource.from_contents(SCHEMA)),
    )
    claims.validate(claims_of(token))
    registration = authority.register("fixture", token, body(execution))
    Draft202012Validator(SCHEMA, registry=registry).validate(asdict(registration))
    with pytest.raises(Exception):
        Draft202012Validator(SCHEMA, registry=registry).validate({**asdict(registration), "extra": 1})
    assert replace(registration, project_ids=[]).session_grant().project_ids == frozenset()


def test_tokens_carry_canonical_unpadded_claims(authority, execution):
    token = issue(authority, execution)
    assert token.split(".")[1] == encoded(claims_of(token))
    assert "=" not in token
    assert token.startswith("v2.")


def test_a_minimal_body_registers_and_a_partial_body_is_still_checked(store, authority, execution):
    token = issue(authority, execution)
    minimal = {"execution_id": execution.execution_id, "generation": 1}
    with pytest.raises(GrantRefused) as error:
        authority.register("fixture", token, {**minimal, "brain_id": "personal-brain"})
    assert str(error.value) == "registration brain_id is outside the launch grant"
    with pytest.raises(GrantRefused) as error:
        authority.register("fixture", token, {**minimal, "project_ids": list(reversed(["local:a", *PROJECTS]))})
    assert str(error.value) == "registration project_ids is outside the launch grant"
    assert authority.register("fixture", token, minimal).execution_id == execution.execution_id


def test_an_odd_length_payload_is_malformed(store, authority, execution):
    with pytest.raises(GrantRefused) as error:
        authority.register("fixture", signed("abcde"), body(execution))
    assert str(error.value) == "launch grant is malformed"


class Flaky:
    def __init__(self, pipe, failures):
        self.pipe = pipe
        self.failures = failures

    def __enter__(self):
        self.pipe.__enter__()
        return self

    def __exit__(self, *args):
        return self.pipe.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self.pipe, name)

    def execute(self):
        from redis.exceptions import WatchError

        if self.failures[0]:
            self.failures[0] -= 1
            raise WatchError("fixture conflict")
        return self.pipe.execute()


def flaky(store, monkeypatch, failures):
    real = store.redis.pipeline
    counter = [failures]
    calls = []

    def pipeline(*args, **kwargs):
        calls.append(1)
        return Flaky(real(*args, **kwargs), counter)

    monkeypatch.setattr(store.redis, "pipeline", pipeline)
    return calls


@pytest.mark.parametrize("operation", ["register", "revoke"])
def test_writes_retry_a_concurrent_change_then_give_up(store, authority, execution, monkeypatch, operation):
    token = issue(authority, execution)

    def run():
        if operation == "register":
            return authority.register("fixture", token, body(execution)).execution_id
        return authority.revoke_outstanding("fixture")

    calls = flaky(store, monkeypatch, 4)
    assert run() == (execution.execution_id if operation == "register" else [claims_of(token)["grant_id"]])
    assert len(calls) == 5
    other = admit(store, "eng-2@fixture")
    token = issue(authority, other)
    execution = other
    calls = flaky(store, monkeypatch, 5)
    with pytest.raises(SwarmError, match="kept changing"):
        run()
    assert len(calls) == 5


def test_revocation_lists_every_outstanding_grant_in_order(store, authority, execution):
    other = admit(store, "eng-2@fixture")
    grants = sorted(claims_of(issue(authority, agent))["grant_id"] for agent in (execution, other, execution))
    assert authority.revoke_outstanding("fixture") == grants
    rows = store.redis.hgetall(store.key("fixture", "launch-grants"))
    assert {json.loads(row)["state"] for row in rows.values()} == {"revoked"}


def test_the_default_clock_is_wall_time(store):
    import time

    assert LaunchAuthority(store, KEY, FIXTURE["issuer"], FIXTURE["audience"]).clock is time.time
