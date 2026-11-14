import json
from typing import Any

import pytest

from scripts.swarm import preempt, profile_first, seat_spawn, timing
from scripts.swarm import tick as tick_module
from scripts.swarm.store import AgentRecord
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


@pytest.mark.feature("swarm-preempt-claim-view-once-per-pass")
def test_a_preempt_pass_builds_one_claim_view_for_every_full_lane(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    owned, calls, read = tick_module._owned_tasks, [], []
    monkeypatch.setattr(tick_module, "_owned_tasks", lambda *args: calls.append(args) or owned(*args))
    rows = ledger()
    doc = rows.state("sw")
    for name, lane in (("e1", "eng"), ("e2", "eng"), ("c1", "ci")):
        store.put_agent("sw", AgentRecord(name, lane, f"held-{name}"))

    def ready(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        found = tick_module.ready_in_order(*args, **kwargs)
        read.append((args, kwargs["claims"], ids(found)))
        return found

    seats = preempt.Seats(rows.rows, lambda *a: False, lambda *a: True, ready, preempt._places, tick_module.claim_view)
    preempt.run("sw", store.config("sw"), store, doc, 1, seats)
    assert calls == [("sw", store)]
    assert [args[4] for args, _, _ in read] == ["eng", "ci"]
    assert read[0][1] is read[1][1]
    monkeypatch.setattr(tick_module, "_owned_tasks", owned)
    assert [ids(tick_module.ready_in_order(*args)) for args, _, _ in read] == [found for _, _, found in read]
    assert all(found for _, _, found in read)
