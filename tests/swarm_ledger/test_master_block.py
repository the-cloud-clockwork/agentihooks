from copy import deepcopy
from types import SimpleNamespace

import pytest

from scripts.swarm import readiness, tick
from scripts.swarm_ledger import ledger_tasks
from scripts.swarm_ledger.ledger_core import Context


def update(doc, ctx, by, **fields):
    op = {"op": "task_update", "id": "u1", "by": by, "item": "tasks/t1", "fields": fields}
    if by != "operator":
        ledger_tasks.check(op)
    assert ledger_tasks.apply(doc, op, ctx)
    assert not ctx.refused


def ledger():
    task = {
        "id": "t1",
        "title": "Queue the suite",
        "lane": "ci",
        "kind": "code",
        "state": "pr",
        "claimed_by": "ci@a1b2c3-0001",
    }
    return {"tasks": [task], "phases": []}, Context({"rev": 0, "stamps": {}}, 1)


@pytest.mark.parametrize("holder", ["master@a1b2c3-0001", "operator"])
@pytest.mark.parametrize("writer", ["ci@a1b2c3-0001", "swarm", "dispatcher"])
@pytest.mark.parametrize("state", ["pr", "open", "claimed"])
def test_authoritative_block_survives_state_writes(holder, writer, state):
    doc, ctx = ledger()
    update(doc, ctx, holder, state="blocked")
    held = deepcopy(doc)
    update(doc, ctx, writer, state=state, claimed_by="ci@a1b2c3-0002")
    assert doc == held


@pytest.mark.parametrize("holder", ["master@a1b2c3-0001", "operator"])
def test_reaffirming_a_worker_block_establishes_the_hold(holder):
    doc, ctx = ledger()
    update(doc, ctx, "ci@a1b2c3-0001", state="blocked")
    update(doc, ctx, holder, state="blocked")
    update(doc, ctx, "swarm", state="open", claimed_by="")
    assert doc["tasks"][0]["state"] == "blocked"


@pytest.mark.parametrize("reopener", ["master@a1b2c3-0002", "operator"])
def test_authorized_reopen_allows_subsequent_claim(reopener):
    doc, ctx = ledger()
    update(doc, ctx, "master@a1b2c3-0001", state="blocked")
    update(doc, ctx, reopener, state="open", claimed_by="")
    update(doc, ctx, "swarm", state="claimed", claimed_by="ci@a1b2c3-0002")
    assert doc["tasks"][0]["state"] == "claimed"


def test_worker_block_keeps_automatic_recovery():
    doc, ctx = ledger()
    update(doc, ctx, "ci@a1b2c3-0001", state="blocked")
    update(doc, ctx, "swarm", state="open", claimed_by="")
    assert doc["tasks"][0]["state"] == "open"


def test_incident_worker_publication_cannot_feed_ownerless_recovery():
    doc, ctx = ledger()
    update(doc, ctx, "master@a1b2c3-0001", state="blocked")
    update(doc, ctx, "ci@a1b2c3-0001", state="pr", claimed_by="")
    rows = {"t1": deepcopy(doc["tasks"][0])}

    def guarded_update(slug, task_id, fields, **kwargs):
        update(doc, ctx, "swarm", **fields)
        return doc["tasks"][0]

    client = SimpleNamespace(update_task=guarded_update)
    store = SimpleNamespace(agents=lambda slug: [], claimant=lambda slug, task_id: None)
    actions = tick._orphans("sw", store, client, rows)
    assert actions == []
    assert doc["tasks"][0]["state"] == "blocked"
    assert not readiness.ready(rows["t1"], rows, doc, {}, "")
