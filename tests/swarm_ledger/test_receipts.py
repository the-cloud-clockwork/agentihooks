import json
import sqlite3

from scripts.swarm_ledger.api import mutations, receipts, resources, schemas
from tests.swarm_ledger.test_sqlite import CONTENT, store

CLAIM = {"op": "task_update", "id": "u1", "by": "eng", "item": "tasks/t2", "fields": {"state": "claimed"}}


class Budget:
    def apply(self, doc, op, ctx, apply_op):
        return apply_op(doc, op, ctx)


class SeedReceipts:
    def apply(self, doc, op, ctx, apply_op):
        ctx.meta["api_operations"] = {
            f"old-{n}": {"digest": f"d{n}", "results": {f"old-{n}": True}} for n in range(1000)
        }
        ctx.dirty = True
        return apply_op(doc, op, ctx)


def guarded(repo, operation_id, op):
    doc = repo.current("ledger").state
    target = schemas.target(op)
    guards = {target: resources.resource_revision(doc, target)}
    payload = {"operation_id": operation_id, "ops": [op], "guards": guards}
    return repo.apply_ops("ledger", ops=[op], gate=mutations.GuardedOperations(payload, Budget(), None))


def last_paths(repo):
    with sqlite3.connect(repo.path) as connection:
        stored = connection.execute("SELECT paths FROM row_changes ORDER BY generation DESC LIMIT 1").fetchone()[0]
    return [json.loads(path) for path in json.loads(stored)]


def test_a_task_write_stores_no_unchanged_operation_record_and_serves_the_whole_rebuild(tmp_path):
    reader, writer = store(tmp_path), store(tmp_path)
    reader.create("ledger", CONTENT)
    reader.apply_ops("ledger", ops=[{"op": "sync", "id": "seed"}], gate=SeedReceipts())
    guarded(writer, "warm", {"op": "sync", "id": "warm"})
    reader.current("ledger")
    state, rejected = guarded(writer, "claim", CLAIM)
    assert rejected == []
    assert state["tasks"][1]["state"] == "claimed"
    records = {
        tuple(map(json.dumps, path[:3])) for path in last_paths(writer) if path[:2] == ["_meta", "api_operations"]
    }
    assert len(records) == 1
    served = reader.current("ledger")
    assert served.state == store(tmp_path).current("ledger").state


def test_receipts_fill_free_slots_then_replace_the_oldest_in_place(monkeypatch):
    monkeypatch.setattr(receipts, "LIMIT", 3)
    meta = {}
    for name in ("a", "b", "c", "d"):
        receipts.store(meta, name, f"digest-{name}", {name: True})
    assert meta["api_operations"] == {
        "0": {"seq": 3, "operation_id": "d", "digest": "digest-d", "results": {"d": True}},
        "1": {"seq": 1, "operation_id": "b", "digest": "digest-b", "results": {"b": True}},
        "2": {"seq": 2, "operation_id": "c", "digest": "digest-c", "results": {"c": True}},
    }
    assert receipts.find(meta, "a") == (None, None)
    assert receipts.find(meta, "c") == ("2", meta["api_operations"]["2"])


def test_a_known_operation_keeps_its_slot_and_sequence(monkeypatch):
    monkeypatch.setattr(receipts, "LIMIT", 3)
    meta = {}
    receipts.store(meta, "a", "first", {"x": True})
    receipts.store(meta, "b", "other", {})
    receipts.store(meta, "a", "first", {"x": True, "y": False})
    assert list(meta["api_operations"]) == ["0", "1"]
    assert meta["api_operations"]["0"] == {
        "seq": 0,
        "operation_id": "a",
        "digest": "first",
        "results": {"x": True, "y": False},
    }


def test_receipts_keyed_by_operation_convert_to_slots_keeping_the_newest(monkeypatch):
    monkeypatch.setattr(receipts, "LIMIT", 2)
    legacy = {name: {"digest": f"d-{name}", "results": {name: True}} for name in ("old", "mid", "new")}
    meta = {"api_operations": legacy}
    assert receipts.find(meta, "old") == ("old", legacy["old"])
    receipts.store(meta, "next", "d-next", {})
    assert meta["api_operations"] == {
        "0": {"seq": 2, "operation_id": "next", "digest": "d-next", "results": {}},
        "1": {"seq": 1, "operation_id": "new", "digest": "d-new", "results": {"new": True}},
    }


def test_slotted_receipts_read_back_in_sorted_key_order_keep_their_slots(monkeypatch):
    monkeypatch.setattr(receipts, "LIMIT", 11)
    meta = {}
    for n in range(11):
        receipts.store(meta, f"op-{n}", f"d-{n}", {})
    meta = json.loads(json.dumps(meta, sort_keys=True))
    before = dict(meta["api_operations"])
    receipts.store(meta, "op-10", "d-10", {"again": True})
    assert meta["api_operations"] == {**before, "10": {**before["10"], "results": {"again": True}}}
