import copy
import json
from unittest.mock import patch

import fakeredis
import pytest

from scripts.events import memory
from scripts.events.config import BusConfig
from scripts.inbox import migration
from scripts.inbox.bus import Message, SeatInbox
from scripts.inbox.store import CLOSED, STATES, InboxStore

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]


@pytest.fixture
def source(monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_HIVE_MEMBER", raising=False)
    client = fakeredis.FakeRedis(decode_responses=True)
    for index, state in enumerate(STATES):
        world = "hive" if index % 2 else ""
        store = InboxStore(client, world=world)
        item = store.send("sender", "eng-1@swarm", f"work {state}", item_id=state, ref="ref", task="task")
        client.hset(store.key("item", item.id), mapping={"state": state, "extra": "preserved", "fyi": "True"})
        client.rpush(store.key("history", item.id), json.dumps({"event": "wake", "at": 1, "detail": state}))
    return client


@pytest.fixture
def bus():
    value = memory.open_bus(BusConfig())
    yield value
    value.close()


def test_snapshot_copies_all_open_states_and_worlds_without_rebuilding_indexes(source):
    before = {key: source.dump(key) for key in source.scan_iter()}
    records = migration.snapshot(source)
    assert {record["item"]["state"] for record in records} == set(STATES) - set(CLOSED)
    assert len(records) == 4
    assert {record["world"] for record in records} == {"", "hive"}
    for record in records:
        assert record["item"]["extra"] == "preserved"
        assert len(record["history"]) == 2
        assert json.loads(record["history"][1])["detail"] == record["item"]["state"]
    assert before == {key: source.dump(key) for key in source.scan_iter()}
    assert migration.snapshot(source) == records


def test_import_preserves_every_field_and_ordered_history_and_is_repeatable(source, bus):
    records = migration.snapshot(source)
    report = migration.import_snapshot(records, bus)
    assert report == {"items": 4, "history_entries": 8, "digest": migration.digest(records)}
    assert migration.read_records(records, bus) == records
    assert migration.import_snapshot(records, bus) == report
    consumer = SeatInbox(bus, "swarm").consumer("eng-1@swarm", "receiver")
    deliveries = consumer.fetch(20, 0)
    assert {receipt.event.id for receipt in deliveries} == {record["item"]["id"] for record in records}
    assert len(deliveries) == 4
    for receipt in deliveries:
        assert receipt.event.data == {
            "id": receipt.event.id,
            "sender": "sender",
            "text": f"work {receipt.event.id}",
            "ref": "ref",
            "fyi": True,
            "task": "task",
        }
        assert consumer.confirm(receipt)
    assert migration.import_snapshot(records, bus) == report
    assert consumer.fetch(20, 0) == []


def test_import_reuses_existing_seat_envelopes_and_refuses_conflicting_content(source, bus):
    records = migration.snapshot(source)
    item = records[0]["item"]
    mail = SeatInbox(bus, "swarm")
    event = mail.send(item["address"], Message(item["id"], item["sender"], item["text"], "ref", True, "task"))
    migration.import_snapshot(records, bus)
    assert mail.send(item["address"], Message(item["id"], item["sender"], item["text"], "ref", True, "task")) == event
    changed = copy.deepcopy(records)
    changed[0]["history"].append('{"event":"changed"}')
    with pytest.raises(ValueError, match="different migration record"):
        migration.import_snapshot(changed, bus)


def test_partial_publish_failure_can_resume_without_losing_history(source, bus):
    records = migration.snapshot(source)
    original = bus.events.publish
    with patch.object(bus.events, "publish", side_effect=RuntimeError("offline")):
        with pytest.raises(RuntimeError, match="offline"):
            migration.import_snapshot(records, bus)
    with patch.object(bus.events, "publish", wraps=original):
        assert migration.import_snapshot(records, bus)["items"] == 4
    assert migration.read_records(records, bus) == records


def test_copy_rehearsal_verifies_the_same_snapshot_on_a_real_nats_server(source, tmp_path):
    from scripts.events.nats import open_bus
    from tests.events.nats_server import running

    records = migration.snapshot(source)
    before = migration.digest(records)
    with running(tmp_path / "copy") as url:
        bus = open_bus(BusConfig(backend="nats", url=url))
        try:
            proof = migration.rehearse(records, bus, url)
            assert proof["digest"] == before
            assert proof["items"] == 4
            assert proof["history_entries"] == 8
            assert migration.read_records(records, bus) == records
        finally:
            bus.close()
        bus = open_bus(BusConfig(backend="nats", url=url))
        try:
            assert migration.read_records(records, bus) == records
        finally:
            bus.close()
    assert migration.digest(migration.snapshot(source)) == before


def test_live_import_is_disabled_by_default_and_requires_unchanged_rehearsed_source(source, bus):
    records = migration.snapshot(source)
    proof = migration.rehearse(records, bus, "copy")
    with pytest.raises(ValueError, match="disabled"):
        migration.apply(source, bus, proof, "live")
    with pytest.raises(ValueError, match="copy destination"):
        migration.apply(source, bus, proof, "copy", enabled=True)
    changed = dict(proof, digest="wrong")
    with pytest.raises(ValueError, match="changed since rehearsal"):
        migration.apply(source, bus, changed, "live", enabled=True)
    live = memory.open_bus(BusConfig())
    try:
        assert migration.apply(source, live, proof, "live", enabled=True)["items"] == 4
        assert migration.read_records(records, live) == records
    finally:
        live.close()


def test_corrupt_destination_fails_verification(source, bus):
    records = migration.snapshot(source)
    migration.import_snapshot(records, bus)
    with patch.object(bus.state("inbox_legacy"), "get", return_value=None):
        with pytest.raises(ValueError, match="verification"):
            migration.read_records(records, bus)


def test_same_id_in_different_worlds_cannot_hide_different_open_items(source, bus):
    records = migration.snapshot(source)
    duplicate = copy.deepcopy(records[0])
    duplicate["world"] = "other"
    duplicate["item"]["text"] = "different"
    with pytest.raises(ValueError, match="item id"):
        migration.import_snapshot([records[0], duplicate], bus)


def test_cli_refuses_live_import_before_connecting_without_enable_flag(tmp_path):
    with patch.object(migration, "open_bus") as opener:
        with pytest.raises(SystemExit) as error:
            migration.main(["apply", str(tmp_path / "proof.json")])
    assert error.value.code == 2
    opener.assert_not_called()
