import re
from pathlib import Path
from types import SimpleNamespace

from scripts.events.config import BIND_ENV, SCOPED_ENV, SCOPED_STATE_BUCKETS
from scripts.events.dead import DEAD_STREAM
from scripts.events.nats import EVENT_STREAM, queue_names
from scripts.events.nats_legacy import LEGACY_EVENTS
from scripts.events.nats_state import ScopedStateStore
from scripts.events.scope import LANES
from scripts.events.subjects import consumer_name, slot_request, stream_name, subject
from scripts.hive import nats_grant
from scripts.hive.auth import NATS_CREDS_FILE
from scripts.hub import slots

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "reference" / "swarm-bus-scope.md"


def test_doc_names_the_settings_buckets_lanes_and_streams_the_code_uses():
    text = DOC.read_text()
    for name in (SCOPED_ENV, BIND_ENV, nats_grant.SIGNER_ENV, EVENT_STREAM, DEAD_STREAM, NATS_CREDS_FILE):
        assert f"`{name}`" in text
    for name in (*SCOPED_STATE_BUCKETS, *slots.BUCKETS, slots.QUEUE, *LANES, *LEGACY_EVENTS):
        assert f"`{name}`" in text
    assert f"`{slot_request('rig', 'm1').replace('.rig.', '.<swarm>.').replace('.m1', '.<hive>')}`" in text
    assert stream_name("rig", "EVENTS") == "AGENTIHOOKS-726967-EVENTS"
    assert "`726967`" in text
    assert stream_name("other", "STATE_spoke-work") in text


def test_doc_lists_every_jetstream_grant_of_a_scoped_member():
    text = DOC.read_text()
    prefix = stream_name("rig", "EVENTS").removesuffix("EVENTS")
    shapes = {
        re.sub(rf"(?:{re.escape(prefix)}|AGENTIHOOKS_726967_)[^.]+", "<stream>", permission).replace(
            "swarm.rig.", "swarm.<swarm>."
        )
        for permission in nats_grant.swarm_publish("rig")
    }
    assert len(shapes) == 6
    section = text.split("## What a scoped member is granted\n", 1)[1].split("\n## ", 1)[0]
    assert "for each of the swarm's streams above: " in section
    for shape in shapes:
        assert f"`{shape}`" in section


def _documented_rows():
    swarm, bucket, key = "rig", "spoke-work", "claim"
    queue = queue_names(f"swarm.{swarm}.ready.eng", swarm)
    state = ScopedStateStore._subject(SimpleNamespace(_swarm=swarm, _bucket=bucket), key)
    rows = {
        "events": (stream_name(swarm, "EVENTS"), f"{subject(swarm, 'events')}.>"),
        "dead": (stream_name(swarm, "DEAD"), f"{subject(swarm, 'dead')}.>"),
        "queue": (queue.stream, queue.filter, queue.subject),
        "state": (stream_name(swarm, f"STATE_{bucket}"), state),
    }
    placeholders = (
        (f"-{swarm.encode().hex()}-", "-<hex>-"),
        (consumer_name("QUEUE_eng"), "h-<queue-digest>"),
        (consumer_name(f"STATE_{bucket}"), "h-<state-digest>"),
        (f".{swarm}.", ".<swarm>."),
        ("_eng", "_<lane>"),
        (".eng.", ".<lane>."),
        (f"_{bucket}", "_<bucket>"),
        (f".{bucket}.{key}", ".<bucket>.<key>"),
    )
    shaped = {}
    for kind, row in rows.items():
        names = []
        for name in row:
            for concrete, placeholder in placeholders:
                name = name.replace(concrete, placeholder)
            names.append(name)
        shaped[kind] = tuple(names)
    return shaped


def test_doc_table_scopes_every_stream_and_subject_to_the_member_swarm():
    section = DOC.read_text().split("## Subjects and streams per swarm\n", 1)[1].split("\n## ", 1)[0]
    table = [line for line in section.splitlines() if line.startswith("| ")]
    for kind, names in _documented_rows().items():
        assert all(name.startswith(("AGENTIHOOKS-<hex>-", "swarm.<swarm>.")) for name in names), kind
        assert any(all(f"`{name}`" in line for name in names) for line in table), (kind, names)


def test_doc_backticks_every_foreign_subject_the_check_refuses():
    section = DOC.read_text().split("## Check a member's scope\n", 1)[1].split("\n## ", 1)[0]
    foreign = (
        f"{subject('other', 'dead')}.<id>",
        ScopedStateStore._subject(SimpleNamespace(_swarm="other", _bucket="spoke-work"), "claim").replace(
            ".spoke-work.claim", ".<bucket>.<key>"
        ),
        f"$JS.API.STREAM.MSG.GET.{stream_name('other', 'STATE_spoke-work')}",
    )
    for name in foreign:
        assert f"`{name}`" in section, name
    assert f"pub {queue_names('swarm.other.ready.eng', 'other').subject} " in section


def test_reference_index_and_hives_page_link_the_doc():
    assert "(swarm-bus-scope.md)" in (ROOT / "docs" / "reference" / "index.md").read_text()
    assert "(../reference/swarm-bus-scope.md)" in (ROOT / "docs" / "pillars" / "hives.md").read_text()
