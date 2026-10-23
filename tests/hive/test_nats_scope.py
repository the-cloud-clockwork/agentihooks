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
