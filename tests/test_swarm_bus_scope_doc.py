import re
from pathlib import Path

from scripts.events.config import BIND_ENV, SCOPED_ENV, SCOPED_STATE_BUCKETS, SHARED_SLOT_BUCKETS
from scripts.events.dead import DEAD_STREAM
from scripts.events.nats import EVENT_STREAM
from scripts.events.nats_legacy import LEGACY_EVENTS
from scripts.events.scope import LANES
from scripts.events.subjects import stream_name
from scripts.hive import nats_grant
from scripts.hive.auth import NATS_CREDS_FILE

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "reference" / "swarm-bus-scope.md"


def test_doc_names_the_settings_buckets_lanes_and_streams_the_code_uses():
    text = DOC.read_text()
    for name in (SCOPED_ENV, BIND_ENV, nats_grant.SIGNER_ENV, EVENT_STREAM, DEAD_STREAM, NATS_CREDS_FILE):
        assert f"`{name}`" in text
    for name in (*SCOPED_STATE_BUCKETS, *SHARED_SLOT_BUCKETS, *LANES, *LEGACY_EVENTS):
        assert f"`{name}`" in text
    assert stream_name("rig", "EVENTS") == "AGENTIHOOKS_726967_EVENTS"
    assert "`726967`" in text
    assert stream_name("other", "STATE_spoke-work") in text


def test_doc_lists_every_jetstream_grant_of_a_scoped_member():
    text = DOC.read_text()
    prefix = stream_name("rig", "EVENTS").removesuffix("EVENTS")
    shapes = {
        re.sub(rf"{prefix}[^.]+", "<stream>", permission).replace("swarm.rig.", "swarm.<swarm>.")
        for permission in nats_grant.swarm_publish("rig")
    }
    assert len(shapes) == 6
    for shape in shapes:
        assert f"`{shape}`" in text


def test_reference_index_and_hives_page_link_the_doc():
    assert "(swarm-bus-scope.md)" in (ROOT / "docs" / "reference" / "index.md").read_text()
    assert "(../reference/swarm-bus-scope.md)" in (ROOT / "docs" / "pillars" / "hives.md").read_text()
