import pytest

from scripts.events.resource_names import legacy_stream_name
from scripts.events.subjects import stream_name
from scripts.hive import nats_grant

pytestmark = pytest.mark.unit


def _own_bucket(member: str) -> list[str]:
    return [
        f"$KV.scheduler-{member}.>",
        f"$JS.API.STREAM.INFO.KV_scheduler-{member}",
        f"$JS.API.STREAM.MSG.GET.KV_scheduler-{member}",
        f"$JS.API.STREAM.CREATE.KV_scheduler-{member}",
    ]


def test_the_controller_reads_and_writes_the_scheduler_bucket():
    assert "$KV.scheduler.>" in nats_grant.controller_claims()["pub"]["allow"]


def test_an_unscoped_member_reads_and_writes_only_its_own_scheduler_bucket():
    allow = nats_grant.member_claims("UMEMBER", member="m1")["pub"]["allow"]

    assert [subject for subject in allow if "scheduler" in subject] == _own_bucket("m1")


def test_a_scoped_member_reads_and_writes_only_its_own_scheduler_stream():
    allow = nats_grant.member_claims("UMEMBER", ("alpha",), member="m1")["pub"]["allow"]
    streams = [stream_name("alpha", "STATE_scheduler-m1"), legacy_stream_name("alpha", "STATE_scheduler-m1")]

    assert [subject for subject in allow if any(stream in subject for stream in streams)] == [
        subject
        for stream in streams
        for subject in (
            f"$JS.API.STREAM.INFO.{stream}",
            f"$JS.API.STREAM.MSG.GET.{stream}",
            f"$JS.API.STREAM.CREATE.{stream}",
        )
    ]
    assert not [subject for subject in allow if "scheduler-m2" in subject or "STATE_scheduler-m2" in subject]


def test_a_minted_member_credential_names_no_other_member_scheduler_bucket():
    allow = nats_grant.member_claims("UMEMBER", member="m1")["pub"]["allow"]

    assert not [subject for subject in allow if "$KV.scheduler." in subject or "scheduler-m2" in subject]
