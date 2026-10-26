from dataclasses import replace

import pytest

from scripts.events import nkey
from scripts.hive import auth, nats_grant
from tests.hive.test_nats_grant import PUBLIC, _opens, _refresh, _violations
from tests.hive.test_nats_grant import admin as admin
from tests.hive.test_nats_grant import signer as signer

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "subject",
    [
        "swarm.other.queue.eng",
        "swarm.other.dead.event",
        "swarm.other.state.spoke-work.key",
        "swarm.other.execution.started",
        "$JS.API.STREAM.INFO.AGENTIHOOKS_6f74686572_EVENTS",
        "$JS.API.STREAM.MSG.GET.AGENTIHOOKS_6f74686572_STATE_spoke-work",
        "$JS.API.CONSUMER.INFO.AGENTIHOOKS_6f74686572_QUEUE_eng.eng",
        "$JS.API.CONSUMER.MSG.NEXT.AGENTIHOOKS_6f74686572_QUEUE_eng.eng",
        "$JS.ACK.AGENTIHOOKS_6f74686572_QUEUE_eng.eng.1.1.1.1.0",
        "$KV.spoke-work.foreign",
        "dead.other",
    ],
)
def test_scoped_join_credentials_cannot_publish_foreign_resources(admin, signer, subject):
    member = auth.mint(admin, "h1", PUBLIC)
    scoped = replace(signer, member_swarms={member["id"]: ("rig",)})
    credentials = _refresh(admin, scoped, member["ledger_credential"])["nats_credentials"]

    async def act(nc):
        await nc.publish(subject, b"{}")

    assert _violations(scoped, credentials, act, refused=True) == [
        f'nats: permissions violation for publish to "{subject.lower()}"'
    ]


@pytest.mark.parametrize("suffix", ["queue.eng", "dead.event", "state.spoke-work.key", "execution.started"])
def test_scoped_join_credentials_cannot_subscribe_to_foreign_resources(admin, signer, suffix):
    member = auth.mint(admin, "h1", PUBLIC)
    scoped = replace(signer, member_swarms={member["id"]: ("rig",)})
    credentials = _refresh(admin, scoped, member["ledger_credential"])["nats_credentials"]
    subject = f"swarm.other.{suffix}"

    async def act(nc):
        await nc.subscribe(subject)

    assert _violations(scoped, credentials, act, refused=True) == [
        f'nats: permissions violation for subscription to "{subject}"'
    ]


@pytest.mark.parametrize("swarm", ["rig", "second"])
def test_scoped_join_credentials_allow_each_hub_configured_swarm(admin, signer, swarm):
    member = auth.mint(admin, "h1", PUBLIC)
    scoped = replace(signer, member_swarms={member["id"]: ("rig", "second")})
    credentials = _refresh(admin, scoped, member["ledger_credential"])["nats_credentials"]
    assert _opens(scoped, credentials)

    async def act(nc):
        for suffix in ("queue.eng", "dead.event", "state.spoke-work.key", "execution.started"):
            subject = f"swarm.{swarm}.{suffix}"
            subscription = await nc.subscribe(subject)
            await nc.publish(subject, b"own work")
            assert (await subscription.next_msg(timeout=5)).data == b"own work"

    assert _violations(scoped, credentials, act) == []


def test_the_hive_endpoint_refuses_to_mint_for_a_member_the_hub_record_does_not_list(admin, signer):
    member = auth.mint(admin, "h1", PUBLIC)
    scoped = replace(signer, member_swarms={"another-member": ("rig",)})
    grant = _refresh(admin, scoped, member["ledger_credential"])
    assert grant == {
        "error": f"the hub record lists no member swarms for hive member {member['id']}; "
        "it mints no unscoped bus credential"
    }


def test_scoped_claims_keep_the_member_reply_inbox(signer):
    scoped = replace(signer, member_swarms={"m1": ("rig",)})
    jwt, user = nkey.parse_creds(nats_grant.mint(scoped, "m1"))
    assert nkey.claims(jwt)["nats"]["sub"]["allow"] == ["swarm.rig.>", f"_INBOX.{user.public}.>"]


def test_member_credentials_hold_no_grant_on_the_hub_owned_account_buckets():
    from scripts.hive.nats_grant import MEMBER_PUBLISH, member_claims

    claims = member_claims("member", ("rig",), member="m1")
    for granted in (claims["pub"]["allow"], MEMBER_PUBLISH):
        assert not [value for value in granted if "account-slots" in value or "hive-slot-accounts" in value]
    assert not [value for value in claims["pub"]["allow"] if value.startswith("$KV.") or ".KV_" in value]


@pytest.mark.parametrize(
    ("swarms", "slots"), [(None, ["slots.*.m1"]), (("rig", "other"), ["slots.rig.m1", "slots.other.m1"])]
)
def test_a_member_credential_publishes_only_the_slot_subject_of_its_own_hive(swarms, slots):
    from scripts.hive.nats_grant import member_claims

    claims = member_claims("member", swarms, member="m1")
    assert [value for value in claims["pub"]["allow"] if "slots" in value] == slots


def test_scoped_claims_refuse_to_grant_every_hive_slot_subject():
    from scripts.hive.nats_grant import member_claims

    with pytest.raises(ValueError, match=r"^invalid event subject token '\*'$"):
        member_claims("member", ("rig",))


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("kind", ["EVENTS", "DEAD", "QUEUE_eng", "STATE_spoke-work"])
def test_scoped_credentials_use_their_own_stream_and_consumer(signer, kind, legacy):

    import nats as nats_client
    from nats.js import api

    from scripts.events.nats import auth_options
    from tests.hive.test_nats_grant import _user_creds

    scoped = replace(signer, member_swarms={"m1": ("rig",)})
    credentials = nats_grant.mint(scoped, "m1")
    from scripts.events.subjects import stream_name

    stream = f"AGENTIHOOKS_726967_{kind}" if legacy else stream_name("rig", kind)
    subject = "swarm.rig." + kind.lower().replace("_", ".")

    async def act(nc):
        hub = await nats_client.connect(
            signer.url, allow_reconnect=False, **auth_options(_user_creds(signer.account, "hub"))
        )
        try:
            js = hub.jetstream()
            await js.add_stream(name=stream, subjects=[subject])
            await js.add_consumer(stream, config=api.ConsumerConfig(durable_name="member", filter_subject=subject))
            member_js = nc.jetstream()
            assert (await member_js.stream_info(stream)).config.name == stream
            assert (await member_js.consumer_info(stream, "member")).name == "member"
            await member_js.publish(subject, b"own work", stream=stream)
            assert (await member_js.get_last_msg(stream, subject)).data == b"own work"
            consumer = await member_js.pull_subscribe_bind(durable="member", stream=stream)
            [message] = await consumer.fetch(1, timeout=5)
            assert message.data == b"own work"
            await message.ack_sync()
        finally:
            await hub.close()

    assert _violations(scoped, credentials, act) == []


@pytest.mark.parametrize("swarm", ["*", ">", "rig.other", "", "rig other"])
def test_scoped_credentials_refuse_invalid_swarm_tokens(signer, swarm):
    scoped = replace(signer, member_swarms={"m1": (swarm,)})
    with pytest.raises(ValueError, match="invalid event subject token"):
        nats_grant.mint(scoped, "m1")


def test_hub_record_supplies_member_swarm_membership(monkeypatch):
    from scripts import openbao_kv
    from tests.hive.test_nats_grant import BAO_ENV, SIGNER_PATH, _signer_record

    operator, account = nkey.create(nkey.OPERATOR), nkey.create(nkey.ACCOUNT)
    record = {**_signer_record(operator, account), "member_swarms": {"m1": ["rig", "second"]}}
    monkeypatch.setattr(openbao_kv, "read", lambda *_: (record, 1))
    loaded = nats_grant.from_environ({nats_grant.SIGNER_ENV: SIGNER_PATH, **BAO_ENV})
    assert loaded.member_swarms == {"m1": ("rig", "second")}


@pytest.mark.parametrize("membership", [None, [], "rig", {"m1": "rig"}])
def test_hub_record_refuses_malformed_member_swarm_membership(monkeypatch, membership):
    from scripts import openbao_kv
    from tests.hive.test_nats_grant import BAO_ENV, SIGNER_PATH, _signer_record

    operator, account = nkey.create(nkey.OPERATOR), nkey.create(nkey.ACCOUNT)
    record = {**_signer_record(operator, account), "member_swarms": membership}
    monkeypatch.setattr(openbao_kv, "read", lambda *_: (record, 1))
    with pytest.raises(auth.HiveError) as error:
        nats_grant.from_environ({nats_grant.SIGNER_ENV: SIGNER_PATH, **BAO_ENV})
    assert str(error.value) == "member_swarms must map member ids to lists of swarm names"


@pytest.mark.parametrize("signer", [4096], indirect=True)
def test_scoped_credentials_require_more_than_the_default_control_line(signer, tmp_path):
    import nats as nats_client

    from scripts.events import nats
    from scripts.events.config import BusConfig
    from tests.events import nats_server

    scoped = replace(signer, member_swarms={"m1": ("rig",)})
    credentials = nats_grant.mint(scoped, "m1")
    with pytest.raises((nats_client.errors.Error, ConnectionResetError)):
        nats.open_bus(BusConfig(backend="nats", url=signer.url, credentials=credentials, provision=False))
    nats_server.wait_logged(tmp_path / "nats.log", "maximum control line exceeded")


def test_scoped_claims_carry_the_canonical_per_resource_policy(signer):
    scoped = replace(signer, member_swarms={"m1": ("rig",)})
    jwt, user = nkey.parse_creds(nats_grant.mint(scoped, "m1"))
    resources = (
        "EVENTS",
        "DEAD",
        "QUEUE_eng",
        "QUEUE_ci",
        "QUEUE_plan",
        "STATE_spoke-work",
        "STATE_spoke-pending",
        "STATE_hive-presence",
        "STATE_hive-executions",
        "STATE_execution-starts",
    )
    expected = {"swarm.rig.>", "slots.rig.m1"}
    from scripts.events.subjects import stream_name

    for stream in [
        name for resource in resources for name in (f"AGENTIHOOKS_726967_{resource}", stream_name("rig", resource))
    ]:
        expected.update(
            (
                f"$JS.API.STREAM.INFO.{stream}",
                f"$JS.API.STREAM.MSG.GET.{stream}",
                f"$JS.API.CONSUMER.INFO.{stream}.*",
                f"$JS.API.CONSUMER.MSG.NEXT.{stream}.*",
                f"$JS.ACK.{stream}.>",
            )
        )
    claims = nkey.claims(jwt)["nats"]
    assert set(claims) == {"type", "version", "pub", "sub", "subs", "data", "payload"}
    assert set(claims["pub"]) == {"allow"}
    assert set(claims["pub"]["allow"]) == expected
    assert claims["sub"] == {"allow": ["swarm.rig.>", f"_INBOX.{user.public}.>"]}


def test_empty_membership_carries_an_explicit_publish_deny(signer):
    scoped = replace(signer, member_swarms={"m1": ()})
    jwt, user = nkey.parse_creds(nats_grant.mint(scoped, "m1"))
    claims = nkey.claims(jwt)["nats"]
    assert claims["pub"] == {"deny": [">"]}
    assert claims["sub"] == {"allow": [f"_INBOX.{user.public}.>"]}


@pytest.mark.parametrize("memberships", [{}, {"another": ("rig",)}])
def test_unlisted_membership_mints_no_credential(signer, memberships):
    scoped = replace(signer, member_swarms=memberships)
    with pytest.raises(auth.HiveError) as error:
        nats_grant.mint(scoped, "m1")
    assert str(error.value) == (
        "the hub record lists no member swarms for hive member m1; it mints no unscoped bus credential"
    )


@pytest.mark.parametrize(
    "swarm,expected",
    [
        ("*", "member_swarms contains an invalid swarm name (invalid event subject token '*')"),
        (None, "member_swarms contains an invalid swarm name (invalid event subject token None)"),
    ],
)
def test_hub_record_refuses_invalid_member_swarm_tokens(monkeypatch, swarm, expected):
    from scripts import openbao_kv
    from tests.hive.test_nats_grant import BAO_ENV, SIGNER_PATH, _signer_record

    operator, account = nkey.create(nkey.OPERATOR), nkey.create(nkey.ACCOUNT)
    record = {**_signer_record(operator, account), "member_swarms": {"m1": [swarm]}}
    monkeypatch.setattr(openbao_kv, "read", lambda *_: (record, 1))
    with pytest.raises(auth.HiveError) as error:
        nats_grant.from_environ({nats_grant.SIGNER_ENV: SIGNER_PATH, **BAO_ENV})
    assert str(error.value) == expected
