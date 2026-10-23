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


def test_unlisted_member_gets_no_swarm_permissions(admin, signer):
    member = auth.mint(admin, "h1", PUBLIC)
    scoped = replace(signer, member_swarms={"another-member": ("rig",)})
    credentials = _refresh(admin, scoped, member["ledger_credential"])["nats_credentials"]

    async def act(nc):
        await nc.publish("swarm.rig.queue.eng", b"{}")

    assert _violations(scoped, credentials, act, refused=True) == [
        'nats: permissions violation for publish to "swarm.rig.queue.eng"'
    ]


def test_scoped_claims_keep_the_member_reply_inbox(signer):
    scoped = replace(signer, member_swarms={"m1": ("rig",)})
    jwt, user = nkey.parse_creds(nats_grant.mint(scoped, "m1"))
    assert nkey.claims(jwt)["nats"]["sub"]["allow"] == ["swarm.rig.>", f"_INBOX.{user.public}.>"]


@pytest.mark.parametrize("kind", ["EVENTS", "DEAD", "QUEUE_eng", "STATE_spoke-work"])
def test_scoped_credentials_use_their_own_stream_and_consumer(signer, kind):

    import nats as nats_client
    from nats.js import api

    from scripts.events.nats import auth_options
    from tests.hive.test_nats_grant import _user_creds

    scoped = replace(signer, member_swarms={"m1": ("rig",)})
    credentials = nats_grant.mint(scoped, "m1")
    stream = f"AGENTIHOOKS_726967_{kind}"
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


@pytest.mark.parametrize("membership", [None, [], "rig", {"m1": "rig"}, {"m1": ["*"]}, {"m1": [None]}])
def test_hub_record_refuses_malformed_member_swarm_membership(monkeypatch, membership):
    from scripts import openbao_kv
    from tests.hive.test_nats_grant import BAO_ENV, SIGNER_PATH, _signer_record

    operator, account = nkey.create(nkey.OPERATOR), nkey.create(nkey.ACCOUNT)
    record = {**_signer_record(operator, account), "member_swarms": membership}
    monkeypatch.setattr(openbao_kv, "read", lambda *_: (record, 1))
    with pytest.raises(auth.HiveError, match="member_swarms"):
        nats_grant.from_environ({nats_grant.SIGNER_ENV: SIGNER_PATH, **BAO_ENV})


@pytest.mark.parametrize("signer", [4096], indirect=True)
def test_scoped_credentials_require_more_than_the_default_control_line(signer):
    import nats as nats_client

    from scripts.events import nats
    from scripts.events.config import BusConfig

    scoped = replace(signer, member_swarms={"m1": ("rig",)})
    credentials = nats_grant.mint(scoped, "m1")
    with pytest.raises(nats_client.errors.Error) as error:
        nats.open_bus(BusConfig(backend="nats", url=signer.url, credentials=credentials, provision=False))
    assert str(error.value) == "nats: 'maximum control line exceeded'"
