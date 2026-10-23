import json
import sqlite3

from scripts.swarm_ledger.api import mutations, resources, schemas
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
