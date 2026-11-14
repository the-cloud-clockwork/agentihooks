"""A hive member reads and settles only its own execution stops, on the swarm's dedicated stop stream."""

from dataclasses import replace
from typing import Any

import pytest

from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.hive import execution_stop, nats_grant, stop_names
from tests.hive.test_nats_grant import _publish_refusals, _signed, _violations
from tests.hive.test_nats_grant import signer as signer

pytestmark = pytest.mark.unit

SWARM = "rig"


def _member(signer: nats_grant.Signer, member: str) -> str:
    return nats_grant.mint(replace(signer, member_swarms={member: (SWARM,)}), member)


def _bus(signer: nats_grant.Signer, credentials: str, provision: bool) -> Any:
    from scripts.events import nats

    return nats.open_bus(
        BusConfig(backend="nats", url=signer.url, credentials=credentials, provision=provision, scoped=True)
    )


def _stop(hive: str) -> Envelope:
    return Envelope(
        id=f"stop:{hive}",
        type=execution_stop.TYPE,
        swarm=SWARM,
        entity="execution",
        key=f"execution-{hive}",
        version=1,
        time=1,
        author="hub",
        data={"name": "agent", "hive": hive, "reason": "stopped"},
    )


def _hub_sends_stops(signer: nats_grant.Signer, *hives: str) -> None:
    hub = _bus(signer, _signed(signer, "hub", nats_grant.controller_claims()), provision=True)
    try:
        stops = hub.for_swarm(SWARM).stops
        for hive in hives:
            stops.consumer(
                stop_names.durable(SWARM, hive), execution_stop.stop_subject(SWARM, hive), execution_stop.POLICY
            )
            assert stops.publish(execution_stop.stop_subject(SWARM, hive), _stop(hive))
    finally:
        hub.close()


@pytest.mark.feature("hive-own-stop-consumer")
def test_a_member_fetches_and_acks_the_stops_on_its_own_stop_consumer(signer: nats_grant.Signer) -> None:
    _hub_sends_stops(signer, "m1", "h2")
    member = _bus(signer, _member(signer, "m1"), provision=False)
    try:
        own = member.for_swarm(SWARM).stops.consumer(
            stop_names.durable(SWARM, "m1"), execution_stop.stop_subject(SWARM, "m1"), execution_stop.POLICY
        )
        [delivery] = own.fetch(10, 2000)
        delivery.ack()
    finally:
        member.close()

    assert delivery.event.data["hive"] == "m1"


@pytest.mark.feature("hive-own-stop-consumer")
@pytest.mark.parametrize("verb", ["$JS.API.CONSUMER.MSG.NEXT", "$JS.API.CONSUMER.INFO"])
def test_a_member_fetch_on_another_hives_stop_consumer_is_denied(signer: nats_grant.Signer, verb: str) -> None:
    _hub_sends_stops(signer, "m1", "h2")
    foreign = f"{verb}.{stop_names.stream(SWARM)}.{stop_names.consumer(SWARM, 'h2')}"

    assert _publish_refusals(signer, _member(signer, "m1"), foreign) == [
        f'nats: permissions violation for publish to "{foreign.lower()}"'
    ]


@pytest.mark.feature("hive-own-stop-consumer")
def test_a_member_ack_on_another_hives_stop_consumer_is_denied(signer: nats_grant.Signer) -> None:
    foreign = f"$JS.ACK.{stop_names.stream(SWARM)}.{stop_names.consumer(SWARM, 'h2')}.1.1.1.1.0"

    assert _publish_refusals(signer, _member(signer, "m1"), foreign) == [
        f'nats: permissions violation for publish to "{foreign.lower()}"'
    ]


@pytest.mark.feature("hive-own-stop-consumer")
@pytest.mark.parametrize("hive", ["m1", "h2"])
def test_a_member_cannot_publish_onto_the_stop_stream(signer: nats_grant.Signer, hive: str) -> None:
    wire = f"swarm.{SWARM}.stop.execution.stop.{hive}"

    assert _publish_refusals(signer, _member(signer, "m1"), wire) == [
        f'nats: permissions violation for publish to "{wire}"'
    ]


@pytest.mark.feature("hive-own-stop-consumer")
@pytest.mark.parametrize("hive", ["h2", "*", ">"])
def test_a_member_subscribe_on_a_stop_subject_is_denied(signer: nats_grant.Signer, hive: str) -> None:
    wire = f"swarm.{SWARM}.stop.execution.stop.{hive}"

    async def act(nc: Any) -> None:
        await nc.subscribe(wire)

    assert _violations(signer, _member(signer, "m1"), act, refused=True) == [
        f'nats: permissions violation for subscription to "{wire}"'
    ]


@pytest.mark.feature("hive-own-stop-consumer")
def test_the_scoped_member_grant_denies_every_stop_subscription() -> None:
    claims = nats_grant.member_claims("member", (SWARM,), member="m1")

    assert claims["sub"]["deny"] == ["swarm.*.stop.>"]


@pytest.mark.feature("hive-own-stop-consumer")
def test_the_scoped_member_grant_names_only_its_own_stop_consumer() -> None:
    claims = nats_grant.member_claims("member", (SWARM, "web"), member="m1")
    stops = [subject for subject in claims["pub"]["allow"] if "-STOP" in subject]

    assert stops == [
        subject
        for swarm in (SWARM, "web")
        for subject in (
            f"$JS.API.STREAM.INFO.{stop_names.stream(swarm)}",
            f"$JS.API.CONSUMER.INFO.{stop_names.stream(swarm)}.{stop_names.consumer(swarm, 'm1')}",
            f"$JS.API.CONSUMER.MSG.NEXT.{stop_names.stream(swarm)}.{stop_names.consumer(swarm, 'm1')}",
            f"$JS.ACK.{stop_names.stream(swarm)}.{stop_names.consumer(swarm, 'm1')}.>",
        )
    ]
    assert claims["pub"]["deny"] == list(nats_grant.MEMBER_DENY)
    assert "swarm.*.stop.>" in nats_grant.MEMBER_DENY


@pytest.mark.feature("hive-own-stop-consumer")
def test_each_swarm_and_hive_has_its_own_stop_consumer_name() -> None:
    names = {stop_names.consumer(swarm, hive) for swarm in ("rig", "web") for hive in ("m1", "h2")}

    assert len(names) == 4
    assert stop_names.consumer(SWARM, "m1") == stop_names.durable(SWARM, "m1")
    assert stop_names.stream(SWARM) == "AGENTIHOOKS-726967-STOP"
