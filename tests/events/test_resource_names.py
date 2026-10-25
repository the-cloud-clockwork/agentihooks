import hashlib
import re

import pytest

from scripts.events import resource_names as names


@pytest.mark.parametrize("value", ["work", "Work-9-a", "x" * 64])
def test_clean_consumer_names_stay_readable(value):
    assert names.consumer_name(value) == value
    assert names.queue_stream(value) == "AGENTIHOOKS-QUEUE-" + value


@pytest.mark.parametrize(
    "value", ["", "lane.eng", "lane_eng", "lane eng", "*", ">", "a--b", "-a", "a-", "ñ", "x" * 256]
)
def test_unsafe_consumer_names_use_the_full_digest(value):
    digest = hashlib.sha256(value.encode()).hexdigest()
    expected = "h-" + digest
    assert names.consumer_name(value) == expected
    assert names.queue_stream(value) == "AGENTIHOOKS-QUEUE-h" + digest
    assert re.fullmatch(r"[A-Za-z0-9-]+", names.queue_stream(value))


def test_a_digest_cannot_alias_a_readable_name():
    digest = names.consumer_name("lane.eng")
    assert len(digest) == 66
    assert names.consumer_name(digest) != digest


def test_legacy_aliases_match_stored_names():
    assert names.legacy_queue_stream("work_1") == "AGENTIHOOKS_QUEUE_work_1"
    assert (
        names.legacy_queue_stream("swarm.rig.ready.eng")
        == "AGENTIHOOKS_QUEUE_h" + hashlib.sha256(b"swarm.rig.ready.eng").hexdigest()
    )
    assert names.legacy_stream_name("rig", "STATE_spoke-work") == "AGENTIHOOKS_726967_STATE_spoke-work"
