import json

import pytest

from scripts.swarm import profile_first, seat_spawn, timing
from scripts.swarm import tick as tick_module
from tests.swarm import test_tick

pytestmark = pytest.mark.unit
store = test_tick.store
FakeLedger, FakeRuntime = test_tick.FakeLedger, test_tick.FakeRuntime


def ledger():
    return FakeLedger(
        [
            {"id": "t1", "lane": "eng", "territory": ["a"]},
            {"id": "t2", "lane": "eng", "territory": ["a"]},
            {"id": "t3", "lane": "ci", "territory": ["a"]},
            {"id": "t4", "lane": "eng"},
            {"id": "t5", "lane": "ci"},
        ]
    )


def ids(found):
    return [task["id"] for task in found]


def test_a_spawn_order_pass_builds_one_claim_view_for_every_lane(store, monkeypatch):
    owned, calls = tick_module._owned_tasks, []
    monkeypatch.setattr(tick_module, "_owned_tasks", lambda *args: calls.append(args) or owned(*args))
    rows = ledger()
    order, _ = tick_module._spawn_order("sw", store.config("sw"), store, [], rows.rows, rows.state("sw"), FakeRuntime())
    assert calls == [("sw", store)]
    assert [(lane, task["id"]) for _, lane, task in order] == [("eng", "t1"), ("ci", "t3"), ("eng", "t4")]


def test_ready_in_order_reads_the_same_tasks_from_a_prebuilt_claim_view(store):
    rows = ledger()
    doc = rows.state("sw")
    claims = tick_module.claim_view("sw", store, rows.rows, doc)
    assert ids(tick_module.ready_in_order("sw", store, rows.rows, doc, "eng", claims=claims)) == ["t1", "t4", "t2"]
    assert ids(tick_module.ready_in_order("sw", store, rows.rows, doc, "ci", claims=claims)) == ["t3", "t5"]
    assert ids(tick_module.ready_in_order("sw", store, rows.rows, doc, "eng")) == ["t1", "t4", "t2"]
    assert ids(tick_module.ready_in_order("sw", store, rows.rows, doc, "ci")) == ["t3", "t5"]


def test_placement_times_each_task_decision_and_refusal(store, capsys):
    task = {"id": "t1", "lane": "eng"}
    with timing.tick("sw"):
        found, waits = profile_first.placeable("sw", store.config("sw"), store, FakeRuntime(), "eng", [task], 1)
    rows = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith("{")]
    steps = [row["step"] for row in rows if row.get("event") == "swarm_tick_step" and row["phase"] == "finished"]
    assert steps == [timing.named(profile_first.decided), timing.named(seat_spawn.placed_elsewhere)]
    assert (ids(found), waits) == (["t1"], [])
