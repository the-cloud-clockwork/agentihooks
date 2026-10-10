import base64
import hashlib
import hmac
import json
import time
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

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
EXPIRES_AT = "2026-10-08T09:05:00Z"


class Clock:
    def __init__(self, now=FIXTURE["now"]):
        self.now = now

    def __call__(self):
        return self.now


def world(audience=FIXTURE["audience"]):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(FIXTURE["swarm"], "agentihooks", 1, 0))
    clock = Clock()
    return store, clock, LaunchAuthority(store, KEY, FIXTURE["issuer"], audience, clock, FIXTURE["ttl"])


@pytest.fixture
def rig():
    return world()


@pytest.fixture
def store(rig):
    return rig[0]


@pytest.fixture
def clock(rig):
    return rig[1]


@pytest.fixture
def authority(rig):
    return rig[2]


@pytest.fixture
def execution(store):
    return admit(store, FIXTURE["seat"])


def admit(store, seat, previous=""):
    name = store.execution("fixture", previous).name if previous else store.next_name("fixture", "eng")
    return store.start_execution(
        "fixture", AgentRecord(name, "eng", FIXTURE["task"], seat=seat, started_at=123), previous
    )


def issue(authority, execution, **changes):
    return authority.issue("fixture", execution.execution_id, **{**FIXTURE["grant"], **changes})


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
    mac = hmac.new(KEY.secret, f"v2.{payload}".encode(), hashlib.sha256).digest()
    return f"v2.{payload}.{base64.urlsafe_b64encode(mac).rstrip(b'=').decode()}"


def encoded(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def resign(token, **changes):
    return signed(encoded({**claims_of(token), **changes}))


@pytest.mark.parametrize("repeat", range(2))
def test_a_launched_worker_registers_only_its_own_execution_and_corpus(store, authority, execution, repeat):
    token = issue(authority, execution)
    registration = authority.register("fixture", token, body(execution))
    assert asdict(registration) == {
        "grant_id": claims_of(token)["grant_id"],
        "swarm_id": "fixture",
        "execution_id": execution.execution_id,
        "generation": 1,
        "seat_id": "eng-1@fixture",
        "task_id": "task",
        "account": "fixture-account",
        "brain_id": "swarm-brain",
        "project_ids": PROJECTS,
        "issuer": "controller@fixture",
        "audience": "registry@fixture",
        "key_id": "fixture-key-1",
        "registered_at": ISSUED_AT,
    }
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
        "expires_at": EXPIRES_AT,
    }
    assert claims["grant_id"].startswith("lgr-")


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
        "expires_at": EXPIRES_AT,
        "state": "issued",
    }
    dumped = json.dumps({key: store.redis.dump(key).hex() for key in store.redis.scan_iter()})
    for part in token.split(".")[1:]:
        assert part not in dumped
    assert KEY.secret.hex() not in dumped
    authority.register("fixture", token, body(execution))
    assert json.loads(store.redis.hget(store.key("fixture", "launch-grants"), grant_id))["state"] == "registered"


def test_a_replaced_execution_cannot_be_issued_a_grant(store, authority, execution):
    admit(store, FIXTURE["seat"], execution.execution_id)
    with pytest.raises(GrantRefused) as error:
        issue(authority, execution)
    assert (error.value.error_class, str(error.value)) == (
        "stale_generation",
        "execution is not the current attempt of its seat",
    )
    with pytest.raises(SwarmError):
        authority.issue("fixture", "exe-unknown", **FIXTURE["grant"])


def other_audience(ctx):
    issuer = LaunchAuthority(
        ctx.store, KEY, FIXTURE["issuer"], FIXTURE["cases"]["wrong_audience"]["audience"], ctx.clock
    )
    ctx.token = issue(issuer, ctx.execution)


def forged_audit(ctx):
    ctx.token = resign(ctx.token, grant_id=f"lgr-{'0' * 32}")


def other_swarm(ctx):
    ctx.store.create(SwarmConfig("other", "agentihooks", 1, 0))
    ctx.slug = "other"


def disabled_after_issue(ctx):
    ctx.store.redis.set(ctx.store.key("fixture", "launch-grants-disabled"), ISSUED_AT)


def claim(**changes):
    return lambda ctx: setattr(ctx, "token", resign(ctx.token, **changes))


def field(**changes):
    return lambda ctx: setattr(ctx, "request", body(ctx.execution, **changes))


def token_as(value):
    return lambda ctx: setattr(ctx, "token", value(ctx.token) if callable(value) else value)


def drop(name):
    return lambda ctx: ctx.request.pop(name)


MALFORMED = ("unauthenticated", "launch grant is malformed")
CASES = {
    "expired": (
        lambda ctx: setattr(ctx.clock, "now", ctx.clock.now + FIXTURE["cases"]["expired"]["advance"]),
        "unauthenticated",
        "launch grant has expired",
    ),
    "not_yet_valid": (
        lambda ctx: setattr(ctx.clock, "now", ctx.clock.now - 1),
        "unauthenticated",
        "launch grant is not yet valid",
    ),
    "wrong_audience": (other_audience, "unauthenticated", "launch grant is for another audience"),
    "wrong_issuer": (
        lambda ctx: setattr(
            ctx, "verifier", LaunchAuthority(ctx.store, KEY, "controller@other", FIXTURE["audience"], ctx.clock)
        ),
        "unauthenticated",
        "launch grant is from another issuer",
    ),
    "wrong_key": (
        lambda ctx: setattr(
            ctx,
            "verifier",
            LaunchAuthority(
                ctx.store, LaunchKey("fixture-key-1", bytes(32)), FIXTURE["issuer"], FIXTURE["audience"], ctx.clock
            ),
        ),
        "unauthenticated",
        "launch grant signature is invalid",
    ),
    "other_key_id": (claim(key_id="fixture-key-2"), "unauthenticated", "launch grant names another signing key"),
    "tampered": (
        token_as(lambda token: token.replace(token.split(".")[1], encoded({**claims_of(token), "task_id": "other"}))),
        "unauthenticated",
        "launch grant signature is invalid",
    ),
    "non_ascii_signature": (
        token_as(lambda token: token + "é"),
        "unauthenticated",
        "launch grant signature is invalid",
    ),
    "truncated": (token_as(lambda token: token.rsplit(".", 1)[0]), *MALFORMED),
    "not_text": (token_as(None), *MALFORMED),
    "other_prefix": (token_as(lambda token: "v3." + token.split(".", 1)[1]), *MALFORMED),
    "not_base64": (token_as(signed("!!!!")), *MALFORMED),
    "odd_length": (token_as(signed("abcde")), *MALFORMED),
    "not_json": (token_as(signed(base64.urlsafe_b64encode(b"{grant").rstrip(b"=").decode())), *MALFORMED),
    "list_payload": (token_as(lambda token: signed(encoded([claims_of(token)]))), *MALFORMED),
    "missing_claim": (
        token_as(lambda token: signed(encoded({k: v for k, v in claims_of(token).items() if k != "key_id"}))),
        *MALFORMED,
    ),
    "extra_claim": (claim(scope="fleet"), *MALFORMED),
    "version": (claim(schema_version="3.0"), "invalid_request", "unsupported launch grant version"),
    "numeric_time": (claim(issued_at=5), *MALFORMED),
    "word_time": (claim(expires_at="tomorrow"), *MALFORMED),
    "list_grant_id": (claim(grant_id=["lgr"]), *MALFORMED),
    "short_grant_id": (claim(grant_id="lgr-0"), *MALFORMED),
    "nested_projects": (claim(project_ids=[["x"]]), *MALFORMED),
    "empty_projects": (claim(project_ids=[]), *MALFORMED),
    "project_text": (claim(project_ids="github.com/o/a"), *MALFORMED),
    "unknown_project": (claim(project_ids=["unknown"]), *MALFORMED),
    "unsorted_projects": (claim(project_ids=["local:b", "github.com/o/a"]), *MALFORMED),
    "repeated_projects": (claim(project_ids=["local:b", "local:b"]), *MALFORMED),
    "text_generation": (claim(generation="1"), *MALFORMED),
    "bool_generation": (claim(generation=True), *MALFORMED),
    "zero_generation": (claim(generation=0), *MALFORMED),
    "bad_brain": (claim(brain_id="brain id"), *MALFORMED),
    "bad_issuer": (claim(issuer=""), *MALFORMED),
    "not_issued": (forged_audit, "unauthenticated", "launch grant was not issued by this controller"),
    "other_swarm": (other_swarm, "forbidden_scope", "launch grant belongs to another swarm"),
    "mismatched_generation": (
        field(generation=1 + FIXTURE["cases"]["mismatched_generation"]["body_generation_offset"]),
        "forbidden_scope",
        "registration generation is outside the launch grant",
    ),
    "bool_body_generation": (
        field(generation=True),
        "forbidden_scope",
        "registration generation is outside the launch grant",
    ),
    "replaced_execution": (
        lambda ctx: admit(ctx.store, FIXTURE["seat"], ctx.execution.execution_id),
        "stale_generation",
        "launch grant is for a superseded execution",
    ),
    "altered_project_body": (
        field(**FIXTURE["cases"]["altered_project_body"]["body"]),
        "forbidden_scope",
        "registration project_ids is outside the launch grant",
    ),
    "project_tuple": (
        field(project_ids=tuple(PROJECTS)),
        "forbidden_scope",
        "registration project_ids is outside the launch grant",
    ),
    "other_task": (field(task_id="other"), "forbidden_scope", "registration task_id is outside the launch grant"),
    "other_brain": (
        field(brain_id="personal-brain"),
        "forbidden_scope",
        "registration brain_id is outside the launch grant",
    ),
    "other_account": (
        field(account="other-account"),
        "forbidden_scope",
        "registration account is outside the launch grant",
    ),
    "other_execution": (
        field(execution_id="exe-other"),
        "forbidden_scope",
        "registration execution_id is outside the launch grant",
    ),
    "other_seat": (
        field(seat_id="eng-2@fixture"),
        "forbidden_scope",
        "registration seat_id is outside the launch grant",
    ),
    "body_swarm": (field(swarm_id="other"), "forbidden_scope", "registration swarm_id is outside the launch grant"),
    "unknown_field": (field(controller_epoch=4), "invalid_request", "registration names an unsupported identity field"),
    "missing_generation": (
        drop("generation"),
        "invalid_request",
        "registration must name its execution and generation",
    ),
    "not_mapping": (
        lambda ctx: setattr(ctx, "request", ["fixture"]),
        "invalid_request",
        "registration body must be an object",
    ),
    "disabled_after_issue": (disabled_after_issue, "forbidden_scope", "launch grants are disabled for this swarm"),
}


@pytest.mark.parametrize("case", CASES)
def test_a_bad_grant_or_body_fails_before_any_registry_write(store, authority, clock, execution, case):
    mutate, error_class, message = CASES[case]
    ctx = SimpleNamespace(
        store=store,
        clock=clock,
        execution=execution,
        verifier=authority,
        slug="fixture",
        token=issue(authority, execution),
    )
    ctx.request = body(execution)
    mutate(ctx)
    before = protected(store)
    with pytest.raises(GrantRefused) as error:
        ctx.verifier.register(ctx.slug, ctx.token, ctx.request)
    assert (error.value.error_class, str(error.value)) == (error_class, message)
    assert protected(store) == before
    assert launch_grant_rejections(store, ctx.slug) == {error_class: 1}
    assert launch_grant_rejections_total(store, ctx.slug) == 1
    for part in (ctx.token or "").split("."):
        assert len(part) < 3 or part not in json.dumps(error.value.detail())
    assert not store.redis.hgetall(store.key("fixture", "launch-registrations"))


def test_a_refusal_carries_the_request_operation_id_and_retry_class(store, authority, execution):
    for operation_id in ("op-fixture-1", "op-fixture-1", "op-fixture-2"):
        with pytest.raises(GrantRefused) as error:
            authority.register("fixture", "bad", body(execution), operation_id)
        assert error.value.detail() == {
            "error_class": "unauthenticated",
            "operation_id": operation_id,
            "retry": "new_request",
            "message": "launch grant is malformed",
        }
    token = issue(authority, execution)
    for unusable in ("", 7, ["op-x"], token, "op id", "a" * 129):
        with pytest.raises(GrantRefused) as error:
            authority.register("fixture", "bad", body(execution), unusable)
        assert error.value.detail()["operation_id"] == "unknown"
    with pytest.raises(GrantRefused) as error:
        authority.register("fixture", "bad", body(execution))
    assert error.value.operation_id == "unknown"
    with pytest.raises(GrantRefused) as error:
        issue(authority, execution, brain_id="")
    assert error.value.detail()["operation_id"] == "unknown"
    registration = authority.register("fixture", token, body(execution), "op-" + "a" * 125)
    assert registration.execution_id == execution.execution_id
    assert GrantRefused("dependency_unavailable", "fixture").retry == "same_request"


def test_an_execution_outside_the_identifier_grammar_gets_no_grant(store, authority):
    agent = AgentRecord(store.next_name("fixture", "eng"), "eng", "SV2 IDN/04", seat=FIXTURE["seat"], started_at=123)
    execution = store.start_execution("fixture", agent)
    before = protected(store)
    with pytest.raises(GrantRefused) as error:
        issue(authority, execution)
    assert (error.value.error_class, str(error.value)) == (
        "invalid_request",
        "execution task or seat is not an identifier",
    )
    assert protected(store) == before


def test_a_grant_is_valid_until_its_last_second(store, authority, clock, execution):
    token = issue(authority, execution)
    clock.now += FIXTURE["ttl"] - 1
    assert authority.register("fixture", token, body(execution)).execution_id == execution.execution_id


def test_a_retried_registration_returns_the_original_identity(store, authority, clock, execution):
    projects = ["github.com/o/a", "local:b"]
    token = issue(authority, execution, project_ids=list(reversed(projects)))
    first = authority.register("fixture", token, body(execution, project_ids=projects))
    clock.now += 60
    restarted = LaunchAuthority(store, KEY, FIXTURE["issuer"], FIXTURE["audience"], clock, FIXTURE["ttl"])
    before = protected(store)
    assert restarted.register("fixture", token, body(execution, project_ids=projects)) == first
    assert protected(store) == before
    assert first.registered_at == ISSUED_AT
    assert len(store.executions("fixture", FIXTURE["seat"])) == 1
    assert len(store.agents("fixture")) == 1
    with pytest.raises(GrantRefused) as error:
        restarted.register("fixture", token, body(execution, project_ids=list(reversed(projects))))
    assert str(error.value) == "registration project_ids is outside the launch grant"


class Hooked:
    def __init__(self, pipe, hook):
        self.pipe = pipe
        self.hook = hook

    def __enter__(self):
        self.pipe.__enter__()
        return self

    def __exit__(self, *args):
        return self.pipe.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self.pipe, name)

    def execute(self):
        return self.hook(self.pipe)


def hook_pipelines(store, monkeypatch, hook):
    real = store.redis.pipeline
    calls = []

    def pipeline(*args, **kwargs):
        calls.append(1)
        return Hooked(real(*args, **kwargs), hook)

    monkeypatch.setattr(store.redis, "pipeline", pipeline)
    return real, calls


def interrupted(when):
    from redis.exceptions import ConnectionError as TransportLost

    def execute(pipe):
        if when == "before":
            raise TransportLost("fixture transport lost before commit")
        pipe.execute()
        raise TransportLost("fixture transport lost after commit")

    return execute


def conflicting(failures):
    from redis.exceptions import WatchError

    left = [failures]

    def execute(pipe):
        if left[0]:
            left[0] -= 1
            raise WatchError("fixture conflict")
        return pipe.execute()

    return execute


@pytest.mark.parametrize("when", ["before", "after"])
def test_an_interrupted_registration_recovers_on_retry(store, authority, clock, execution, monkeypatch, when):
    from redis.exceptions import ConnectionError as TransportLost

    token = issue(authority, execution)
    before = protected(store)
    real, _calls = hook_pipelines(store, monkeypatch, interrupted(when))
    with pytest.raises(TransportLost):
        authority.register("fixture", token, body(execution))
    monkeypatch.setattr(store.redis, "pipeline", real)
    survived = store.redis.hgetall(store.key("fixture", "launch-registrations"))
    assert (protected(store) == before) is (when == "before")
    assert len(survived) == (0 if when == "before" else 1)
    clock.now += 30
    restarted = LaunchAuthority(store, KEY, FIXTURE["issuer"], FIXTURE["audience"], clock, FIXTURE["ttl"])
    registration = restarted.register("fixture", token, body(execution))
    assert registration.registered_at == ("2026-10-08T09:00:30Z" if when == "before" else ISSUED_AT)
    assert len(store.redis.hgetall(store.key("fixture", "launch-registrations"))) == 1
    assert len(store.executions("fixture", FIXTURE["seat"])) == 1


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
    newer = admit(store, FIXTURE["seat"], execution.execution_id)
    current = authority.register("fixture", issue(authority, newer), body(newer))
    before = protected(store)
    with pytest.raises(GrantRefused) as error:
        authority.register("fixture", old, body(execution))
    assert error.value.error_class == "stale_generation"
    assert protected(store) == before
    assert authority.registration("fixture", newer.execution_id) == current
    assert authority.registration("fixture", execution.execution_id).generation == 1


def test_disabling_revokes_outstanding_grants_and_keeps_history_readable(store, authority, clock, execution):
    token = issue(authority, execution)
    registered = authority.register("fixture", token, body(execution))
    other = admit(store, "eng-2@fixture")
    outstanding = issue(authority, other)
    history = store.executions("fixture", FIXTURE["seat"])
    assert authority.disable("fixture") == [claims_of(outstanding)["grant_id"]]
    assert store.redis.get(store.key("fixture", "launch-grants-disabled")) == ISSUED_AT
    assert authority.disable("fixture") == []
    with pytest.raises(GrantRefused) as error:
        authority.register("fixture", outstanding, body(other))
    assert (error.value.error_class, str(error.value)) == ("unauthenticated", "launch grant was revoked")
    with pytest.raises(GrantRefused) as error:
        issue(authority, other)
    assert (error.value.error_class, str(error.value)) == (
        "forbidden_scope",
        "launch grants are disabled for this swarm",
    )
    assert authority.register("fixture", token, body(execution)) == registered
    assert authority.registration("fixture", execution.execution_id) == registered
    assert authority.registration("fixture", other.execution_id) is None
    assert store.executions("fixture", FIXTURE["seat"]) == history
    authority.enable("fixture")
    fresh = issue(authority, other)
    assert authority.register("fixture", fresh, body(other)).execution_id == other.execution_id


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"project_ids": []}, "a launch grant needs at least one project"),
        ({"project_ids": "github.com/o/a"}, "a launch grant needs at least one project"),
        ({"project_ids": None}, "a launch grant needs at least one project"),
        ({"project_ids": ["unknown"]}, "launch grant project is not a canonical project ID"),
        ({"project_ids": ["agentihooks"]}, "launch grant project is not a canonical project ID"),
        ({"project_ids": [7, "github.com/o/a"]}, "launch grant project is not a canonical project ID"),
        ({"project_ids": [["x"]]}, "launch grant project is not a canonical project ID"),
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
    for projects in (["local:b", "github.com/o/a", "local:b"], ("local:b", "github.com/o/a")):
        assert claims_of(issue(authority, execution, project_ids=projects))["project_ids"] == [
            "github.com/o/a",
            "local:b",
        ]


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
    assert LaunchAuthority(store, KEY, FIXTURE["issuer"], FIXTURE["audience"], clock, 1).ttl == 1
    assert LaunchAuthority(store, KEY, FIXTURE["issuer"], FIXTURE["audience"], clock).ttl == 300
    assert LaunchAuthority(store, KEY, FIXTURE["issuer"], FIXTURE["audience"]).clock is time.time


def test_grant_and_registration_match_the_schema(store, authority, execution):
    from jsonschema import Draft202012Validator, ValidationError
    from referencing import Registry, Resource

    common = json.loads((Path(__file__).parents[2] / "docs/swarm-v2/schemas/common.json").read_text())
    registry = Registry().with_resource(common["$id"], Resource.from_contents(common))
    registry = registry.with_resource(SCHEMA["$id"], Resource.from_contents(SCHEMA))
    token = issue(authority, execution)
    Draft202012Validator({"$ref": "urn:swarm-v2:launch-grant#/$defs/claims"}, registry=registry).validate(
        claims_of(token)
    )
    registration = authority.register("fixture", token, body(execution))
    validator = Draft202012Validator(SCHEMA, registry=registry)
    validator.validate(asdict(registration))
    with pytest.raises(ValidationError):
        validator.validate({**asdict(registration), "extra": 1})
    assert replace(registration, project_ids=[]).session_grant().project_ids == frozenset()


def test_tokens_carry_canonical_unpadded_claims(authority, execution):
    token = issue(authority, execution)
    assert token.split(".")[1] == encoded(claims_of(token))
    assert "=" not in token
    assert token.startswith("v2.")


def test_encoding_strips_only_padding():
    from scripts.swarm_v2.auth_context import _decode, _encode

    assert _encode(bytes([0, 0, 0x17])) == "AAAX"
    assert _encode(b"a") == "YQ"
    for value in ({"a": 1}, {"ab": 1}, {"abc": 1}):
        assert _decode(encoded(value)) == value


def test_times_are_utc_whatever_the_host_zone(monkeypatch):
    from scripts.swarm_v2.auth_context import _seconds, _timestamp

    monkeypatch.setenv("TZ", "Asia/Kolkata")
    time.tzset()
    try:
        assert _timestamp(0) == "1970-01-01T00:00:00Z"
        assert _seconds("1970-01-01T00:00:00Z") == 0
        assert _seconds(_timestamp(FIXTURE["now"])) == FIXTURE["now"]
    finally:
        monkeypatch.undo()
        time.tzset()


def test_a_minimal_body_registers_and_a_partial_body_is_still_checked(store, authority, execution):
    token = issue(authority, execution)
    minimal = {"execution_id": execution.execution_id, "generation": 1}
    with pytest.raises(GrantRefused) as error:
        authority.register("fixture", token, {**minimal, "brain_id": "personal-brain"})
    assert str(error.value) == "registration brain_id is outside the launch grant"
    assert authority.register("fixture", token, minimal).execution_id == execution.execution_id


@pytest.mark.parametrize("operation", ["register", "disable", "issue"])
def test_writes_retry_a_concurrent_change_then_give_up(store, authority, execution, monkeypatch, operation):
    token = issue(authority, execution)

    def run():
        if operation == "register":
            return authority.register("fixture", token, body(execution)).execution_id
        if operation == "issue":
            return claims_of(issue(authority, execution))["execution_id"]
        return authority.disable("fixture")

    _real, calls = hook_pipelines(store, monkeypatch, conflicting(4))
    assert run() == ([claims_of(token)["grant_id"]] if operation == "disable" else execution.execution_id)
    assert len(calls) == 5
    authority.enable("fixture")
    execution = admit(store, "eng-2@fixture")
    token = issue(authority, execution)
    _real, calls = hook_pipelines(store, monkeypatch, conflicting(5))
    with pytest.raises(GrantRefused) as error:
        run()
    assert (
        str(error.value)
        == {
            "register": "launch registrations kept changing; registration was not committed",
            "issue": "launch grants kept changing; the grant was not issued",
            "disable": "launch grants kept changing; revocation was not committed",
        }[operation]
    )
    assert (error.value.error_class, error.value.retry) == ("dependency_unavailable", "same_request")
    assert launch_grant_rejections(store, "fixture") == {"dependency_unavailable": 1}
    assert len(calls) == 5


def test_a_grant_issued_while_disabling_is_refused(store, authority, execution, monkeypatch):
    disabled = store.key("fixture", "launch-grants-disabled")

    def disable_first(pipe):
        if not store.redis.exists(disabled):
            store.redis.set(disabled, ISSUED_AT)
        return pipe.execute()

    hook_pipelines(store, monkeypatch, disable_first)
    with pytest.raises(GrantRefused) as error:
        issue(authority, execution)
    assert (error.value.error_class, str(error.value)) == (
        "forbidden_scope",
        "launch grants are disabled for this swarm",
    )
    assert not store.redis.hgetall(store.key("fixture", "launch-grants"))


def test_a_grant_issued_while_its_execution_is_replaced_is_refused(store, authority, execution, monkeypatch):
    replaced = []

    def replace_first(pipe):
        if not replaced:
            replaced.append(True)
            admit(store, FIXTURE["seat"], execution.execution_id)
        return pipe.execute()

    hook_pipelines(store, monkeypatch, replace_first)
    with pytest.raises(GrantRefused) as error:
        issue(authority, execution)
    assert error.value.error_class == "stale_generation"
    assert not store.redis.hgetall(store.key("fixture", "launch-grants"))


def race_replace(ctx):
    admit(ctx.store, FIXTURE["seat"], ctx.execution.execution_id)


def race_register(ctx):
    other = {**json.loads(ctx.store.redis.hget(ctx.grants, ctx.grant_id)), "grant_id": f"lgr-{'1' * 32}"}
    row = {**body(ctx.execution), "grant_id": other["grant_id"], "issuer": "controller@fixture"}
    row.update(audience="registry@fixture", key_id="fixture-key-1", registered_at=ISSUED_AT)
    ctx.store.redis.hset(ctx.store.key("fixture", "launch-registrations"), ctx.execution.execution_id, json.dumps(row))


def race_revoke(ctx):
    audit = json.loads(ctx.store.redis.hget(ctx.grants, ctx.grant_id))
    ctx.store.redis.hset(ctx.grants, ctx.grant_id, json.dumps({**audit, "state": "revoked"}))


def race_disable(ctx):
    ctx.store.redis.set(ctx.store.key("fixture", "launch-grants-disabled"), ISSUED_AT)


RACES = {
    "executions": (race_replace, "launch grant is for a superseded execution"),
    "registrations": (race_register, "execution is already registered under another launch grant"),
    "grants": (race_revoke, "launch grant was revoked"),
    "disabled": (race_disable, "launch grants are disabled for this swarm"),
}


@pytest.mark.parametrize("race", RACES)
def test_a_registration_retries_when_a_watched_key_changes_first(store, authority, execution, monkeypatch, race):
    token = issue(authority, execution)
    ctx = SimpleNamespace(
        store=store,
        execution=execution,
        grants=store.key("fixture", "launch-grants"),
        grant_id=claims_of(token)["grant_id"],
    )
    write, message = RACES[race]
    raced = []

    def race_first(pipe):
        if not raced:
            raced.append(True)
            write(ctx)
        return pipe.execute()

    hook_pipelines(store, monkeypatch, race_first)
    with pytest.raises(GrantRefused) as error:
        authority.register("fixture", token, body(execution))
    assert str(error.value) == message


def test_disabling_lists_every_outstanding_grant_in_order(store, authority, execution):
    other = admit(store, "eng-2@fixture")
    grants = sorted(claims_of(issue(authority, agent))["grant_id"] for agent in (execution, other, execution))
    assert authority.disable("fixture") == grants
    rows = store.redis.hgetall(store.key("fixture", "launch-grants"))
    assert {json.loads(row)["state"] for row in rows.values()} == {"revoked"}


def registered(authority, execution):
    token = issue(authority, execution)
    authority.register("fixture", token, body(execution))
    return token


def audit_of(store, token):
    return json.loads(store.redis.hget(store.key("fixture", "launch-grants"), claims_of(token)["grant_id"]))


def refusal(action, *args):
    with pytest.raises(GrantRefused) as error:
        action("fixture", *args)
    return error.value.error_class, str(error.value)


def test_a_registered_credential_renews_bound_to_its_registration(store, authority, clock, execution):
    token = registered(authority, execution)
    clock.now += 200
    renewed, expires_at = authority.renew("fixture", token)
    assert expires_at == "2026-10-08T09:08:20Z"
    assert claims_of(renewed) == {**claims_of(token), "issued_at": "2026-10-08T09:03:20Z", "expires_at": expires_at}
    clock.now += 200
    assert authority.bound("fixture", renewed) == authority.registration("fixture", execution.execution_id)
    assert refusal(authority.bound, token) == ("unauthenticated", "launch grant has expired")
    assert audit_of(store, token) == {**audit_of(store, token), "state": "registered", "expires_at": expires_at}
    assert audit_of(store, token)["renewals"] == 1
    again, _ = authority.renew("fixture", renewed)
    assert audit_of(store, again)["renewals"] == 2
    assert renewed.split(".")[1] not in json.dumps(store.redis.hgetall(store.key("fixture", "launch-grants")))


def test_only_a_live_registered_credential_renews(store, authority, clock, execution):
    token = issue(authority, execution)
    assert refusal(authority.renew, token) == ("unauthenticated", "launch grant is not registered")
    authority.register("fixture", token, body(execution))
    admit(store, FIXTURE["seat"], execution.execution_id)
    assert refusal(authority.renew, token) == ("stale_generation", "launch grant is for a superseded execution")
    store, clock, authority = world()
    execution = admit(store, FIXTURE["seat"])
    token = registered(authority, execution)
    clock.now += FIXTURE["ttl"]
    assert refusal(authority.renew, token) == ("unauthenticated", "launch grant has expired")
    assert "renewals" not in audit_of(store, token)


def test_revoking_a_registration_refuses_every_credential_bound_to_it(store, authority, execution):
    token = registered(authority, execution)
    renewed, _ = authority.renew("fixture", token)
    assert authority.revoke("fixture", execution.execution_id) is True
    for credential in (token, renewed):
        assert refusal(authority.bound, credential) == ("unauthenticated", "launch grant was revoked")
        assert refusal(authority.renew, credential) == ("unauthenticated", "launch grant was revoked")
    assert refusal(authority.register, renewed, body(execution)) == ("unauthenticated", "launch grant was revoked")
    assert audit_of(store, token)["state"] == "revoked"
    assert authority.registration("fixture", execution.execution_id).grant_id == claims_of(token)["grant_id"]
    assert authority.revoke("fixture", execution.execution_id) is False
    assert authority.revoke("fixture", "missing") is False


def test_a_renewal_raced_by_revocation_is_refused(store, authority, execution, monkeypatch):
    token = registered(authority, execution)
    raced = []

    def revoke_first(pipe):
        if not raced:
            raced.append(True)
            authority.revoke("fixture", execution.execution_id)
        return pipe.execute()

    hook_pipelines(store, monkeypatch, revoke_first)
    assert refusal(authority.renew, token) == ("unauthenticated", "launch grant was revoked")
    assert audit_of(store, token)["state"] == "revoked"
    assert "renewals" not in audit_of(store, token)


def test_a_grant_rewrite_that_keeps_racing_is_unavailable(store, authority, execution, monkeypatch):
    from redis.exceptions import WatchError

    token = registered(authority, execution)

    def always_raced(pipe):
        raise WatchError("raced")

    hook_pipelines(store, monkeypatch, always_raced)
    assert refusal(authority.renew, token) == (
        "dependency_unavailable",
        "launch grants kept changing; the credential was not renewed",
    )
    assert refusal(authority.revoke, execution.execution_id) == (
        "dependency_unavailable",
        "launch grants kept changing; the registration was not revoked",
    )
