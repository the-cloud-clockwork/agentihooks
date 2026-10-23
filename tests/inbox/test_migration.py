import copy
import json
from unittest.mock import patch

import pytest

from scripts.events import memory
from scripts.events.config import BusConfig
from scripts.inbox import migration
from scripts.inbox.bus import Message, SeatInbox
from scripts.inbox.store import CLOSED, STATES, InboxStore

pytestmark = pytest.mark.unit


@pytest.fixture
def source(monkeypatch):
    import fakeredis

    monkeypatch.delenv("AGENTIHOOKS_HIVE_MEMBER", raising=False)
    client = fakeredis.FakeRedis(decode_responses=True)
    for index, state in enumerate(STATES):
        world = "hive" if index % 2 else ""
        store = InboxStore(client, world=world)
        monkeypatch.setattr(store, "home", lambda address: world)
        item = store.send("sender", "eng-1@swarm", f"work {state}", item_id=state, ref="ref", task="task")
        client.hset(store.key("item", item.id), mapping={"state": state, "extra": "preserved", "fyi": "True"})
        store.about(item.id, "tasks/reference")
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
        assert record["entry"] == "tasks/reference"
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
    with pytest.raises(ValueError, match=r"^item already has a different migration record$"):
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
    with pytest.raises(ValueError, match=r"^live inbox migration is disabled$"):
        migration.apply(source, bus, proof, "live")
    with pytest.raises(ValueError, match=r"^live migration requires a separate copy destination$"):
        migration.apply(source, bus, proof, "copy", enabled=True)
    changed = dict(proof, digest="wrong")
    with pytest.raises(ValueError, match=r"^source changed since rehearsal$"):
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
        with pytest.raises(ValueError, match=r"^migration verification failed$"):
            migration.read_records(records, bus)


def test_same_id_in_different_worlds_cannot_hide_different_open_items(source, bus):
    records = migration.snapshot(source)
    duplicate = copy.deepcopy(records[0])
    duplicate["world"] = "other"
    duplicate["item"]["text"] = "different"
    with pytest.raises(ValueError, match=r"^duplicate item id in migration snapshot$"):
        migration.import_snapshot([records[0], duplicate], bus)


def test_cli_refuses_live_import_before_connecting_without_enable_flag(tmp_path, capsys):
    with patch.object(migration, "open_bus") as opener:
        with patch.object(migration.BusConfig, "from_environ") as config:
            with pytest.raises(SystemExit) as error:
                migration.main(["apply", str(tmp_path / "proof.json")])
    assert error.value.code == 2
    assert capsys.readouterr().err.split(": error: ")[1].strip() == (
        "live inbox migration is disabled; a scheduled cutover requires --enable-live"
    )
    opener.assert_not_called()
    config.assert_not_called()


@pytest.mark.parametrize("backend,url", [("memory", "nats://unused"), ("nats", None)])
def test_cli_requires_an_explicit_nats_destination(backend, url, tmp_path, capsys):
    with patch.object(migration.BusConfig, "from_environ", return_value=BusConfig(backend=backend, url=url)):
        with patch.object(migration, "open_bus") as opener:
            with pytest.raises(SystemExit) as error:
                migration.main(["rehearse", str(tmp_path / "snapshot.json"), "--receipt", str(tmp_path / "proof.json")])
    assert error.value.code == 2
    assert capsys.readouterr().err.split(": error: ")[1].strip() == (
        "migration requires an explicit NATS backend and URL"
    )
    opener.assert_not_called()


def test_cli_snapshots_rehearses_and_imports_a_copy_into_a_separate_nats_destination(source, tmp_path, capsys):
    from scripts.events.nats import open_bus
    from tests.events.nats_server import running

    exported, receipt = tmp_path / "snapshot.json", tmp_path / "proof.json"
    with patch.object(migration, "_source", return_value=source):
        assert migration.main(["snapshot", str(exported)]) == 0
        assert exported.stat().st_mode & 0o777 == 0o600
        assert json.loads(capsys.readouterr().out)["items"] == 4
        records = json.loads(exported.read_text())
        assert records == migration.snapshot(source)
        with pytest.raises(FileExistsError):
            migration.main(["snapshot", str(exported)])
        with running(tmp_path / "copy") as url:
            with patch.object(migration.BusConfig, "from_environ", return_value=BusConfig(backend="nats", url=url)):
                assert migration.main(["rehearse", str(exported), "--receipt", str(receipt)]) == 0
            proof = json.loads(capsys.readouterr().out)
            assert proof == json.loads(receipt.read_text())
            assert proof["version"] == 1
            assert proof["copy"] == migration.digest(url)
            assert receipt.stat().st_mode & 0o777 == 0o600
        with running(tmp_path / "live") as url:
            with patch.object(migration.BusConfig, "from_environ", return_value=BusConfig(backend="nats", url=url)):
                assert migration.main(["apply", str(receipt), "--enable-live"]) == 0
            assert json.loads(capsys.readouterr().out)["history_entries"] == 8
            bus = open_bus(BusConfig(backend="nats", url=url))
            try:
                assert migration.read_records(records, bus) == records
            finally:
                bus.close()


def test_apply_refuses_unknown_proof_versions_and_changes_during_the_move(source, bus):
    records = migration.snapshot(source)
    proof = migration.rehearse(records, bus, "copy")
    with pytest.raises(ValueError, match=r"^unsupported migration proof version$"):
        migration.apply(source, bus, dict(proof, version=2), "live", enabled=True)
    live = memory.open_bus(BusConfig())
    try:
        with patch.object(migration, "snapshot", side_effect=[records, []]):
            with pytest.raises(ValueError, match=r"^source changed during migration; cutover is unverified$"):
                migration.apply(source, live, proof, "live", enabled=True)
    finally:
        live.close()


@pytest.mark.parametrize("state", ["done", "unknown"])
def test_import_refuses_closed_or_unknown_states_before_publishing(source, bus, state):
    records = migration.snapshot(source)
    records[0]["item"]["state"] = state
    with patch.object(bus.events, "publish") as publisher:
        with pytest.raises(ValueError, match=r"^migration requires an open inbox item$"):
            migration.import_snapshot(records, bus)
    publisher.assert_not_called()


def test_destination_aliases_cannot_use_the_rehearsal_store_as_the_live_store(source, tmp_path):
    from scripts.events.nats import open_bus
    from tests.events.nats_server import running

    records = migration.snapshot(source)
    with running(tmp_path / "copy") as url:
        bus = open_bus(BusConfig(backend="nats", url=url))
        try:
            proof = migration.rehearse(records, bus, url)
            assert migration.rehearse(records, bus, url) == proof
        finally:
            bus.close()
        alias = url.replace("127.0.0.1", "localhost")
        bus = open_bus(BusConfig(backend="nats", url=alias))
        try:
            with pytest.raises(ValueError, match=r"^live migration requires a separate copy destination$"):
                migration.apply(source, bus, proof, alias, enabled=True)
        finally:
            bus.close()


def test_snapshot_retains_long_histories_and_handles_repeated_key_separators(source):
    store = InboxStore(source, world="nested:inbox:item:world")
    item = source.hgetall(InboxStore(source, world="").key("item", "pending"))
    source.hset(store.key("item", "pending"), mapping=item)
    history = [json.dumps({"event": "wake", "at": number}) for number in range(5)]
    source.rpush(store.key("history", "pending"), *history)
    records = migration.snapshot(source)
    record = next(record for record in records if record["world"] == "nested:inbox:item:world")
    assert record["history"] == history


@pytest.mark.parametrize(
    "address,swarm", [("standalone-session", "standalone"), ("session@with@nested", "with@nested")]
)
def test_standalone_addresses_remain_deliverable_in_the_standalone_inbox(source, bus, address, swarm):
    records = migration.snapshot(source)[:1]
    records[0]["item"]["address"] = address
    migration.import_snapshot(records, bus)
    receipt = SeatInbox(bus, swarm).consumer(address, "receiver").fetch(1, 0)[0]
    assert receipt.event.key == address
    assert receipt.event.data["id"] == records[0]["item"]["id"]


def test_snapshot_digest_is_stable_across_mapping_order():
    assert migration.digest({"b": 2, "a": 1}) == "d8497d9d82770a70729261095aa98f7ef5154d7af499f8037b6ca250296785a6"
    assert migration.digest({"a": 1, "b": 2}) == "d8497d9d82770a70729261095aa98f7ef5154d7af499f8037b6ca250296785a6"


@pytest.mark.parametrize("args", [[], ["rehearse", "snapshot.json"]])
def test_cli_requires_a_mode_and_a_rehearsal_receipt(args):
    with patch.object(migration, "open_bus") as opener:
        with pytest.raises(SystemExit) as error:
            migration.main(args)
    assert error.value.code == 2
    opener.assert_not_called()


def test_cli_help_explains_the_scheduled_cutover(capsys):
    with pytest.raises(SystemExit) as error:
        migration.main(["--help"])
    assert error.value.code == 0
    assert "Copy and rehearse open Redis inbox items before a scheduled NATS cutover" in " ".join(
        capsys.readouterr().out.split()
    )


def test_the_copy_url_is_refused_even_with_a_fresh_destination_store(source, bus):
    proof = migration.rehearse(migration.snapshot(source), bus, "copy")
    live = memory.open_bus(BusConfig())
    try:
        with pytest.raises(ValueError, match=r"^live migration requires a separate copy destination$"):
            migration.apply(source, live, proof, "copy", enabled=True)
    finally:
        live.close()


def test_snapshot_command_uses_the_configured_redis_source(source, tmp_path, capsys):
    with patch("scripts.swarm.store.redis_client", return_value=source) as connector:
        target = tmp_path / "snapshot.json"
        assert migration.main(["snapshot", str(target)]) == 0
    connector.assert_called_once_with()
    assert json.loads(target.read_text()) == migration.snapshot(source)
    assert json.loads(capsys.readouterr().out)["items"] == 4


def test_import_refuses_an_item_without_identity_before_writing(bus):
    with patch.object(bus.events, "publish") as publisher:
        with pytest.raises(KeyError) as error:
            migration.import_snapshot([{"item": {}}], bus)
    assert error.value.args == ("id",)
    publisher.assert_not_called()
