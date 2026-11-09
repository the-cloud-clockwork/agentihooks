"""The spawn order computes its claim order once per pass and fits its budget over the replayed rig-grade-swarm ledger."""

import gzip
import json
import time
from pathlib import Path
from typing import Any

import pytest

from scripts.swarm import fleet_red, freeze, release_wait
from scripts.swarm import tick as tick_module
from scripts.tick.steps import budgets
from tests.swarm import test_tick

pytestmark = pytest.mark.unit
store = test_tick.store
FakeLedger, FakeRuntime = test_tick.FakeLedger, test_tick.FakeRuntime
REPLAY = Path(__file__).with_name("spawn_pass_replay.json.gz")
BUDGET_SHARE = 33


def replay() -> dict[str, Any]:
    """The rig-grade-swarm ledger of 9 November: 4827 tasks, 997 open, 1322 freeze and focus records."""
    with gzip.open(REPLAY, "rt") as raw:
        return json.load(raw)


def counted(monkeypatch: pytest.MonkeyPatch, name: str) -> list[Any]:
    """Count every call of the swarm tick function ``name`` while still running it."""
    calls: list[Any] = []
    real = getattr(tick_module, name)
    monkeypatch.setattr(tick_module, name, lambda *args: calls.append(args) or real(*args))
    return calls


def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the claim filters off the network: no release tags and no open pull request listings."""
    monkeypatch.setattr(release_wait, "tags", lambda config: [])
    monkeypatch.setattr(fleet_red, "read", lambda repo, *args, **kwargs: ())


@pytest.mark.feature("swarm-spawn-order-once-per-pass")
def test_a_spawn_pass_computes_its_claim_order_once_for_every_lane(store, monkeypatch):
    ledger = FakeLedger(
        [
            {"id": "t1", "lane": "eng"},
            {"id": "t2", "lane": "ci"},
            {"id": "t3", "lane": "plan", "kind": "plan"},
            {"id": "t4", "lane": "eng"},
        ]
    )
    views, orders = counted(monkeypatch, "claim_view"), counted(monkeypatch, "_launch_order")
    order, _ = tick_module._spawn_order(
        "sw", store.config("sw"), store, [], ledger.rows, ledger.state("sw"), FakeRuntime()
    )
    assert (len(views), len(orders)) == (1, 1)
    assert [(lane, task["id"]) for lane, task in order][:3] == [("eng", "t1"), ("ci", "t2"), ("eng", "t4")]


@pytest.mark.feature("swarm-spawn-order-once-per-pass")
def test_a_spawn_pass_over_the_replayed_ledger_fits_its_budget(store, monkeypatch):
    offline(monkeypatch)
    doc = replay()
    rows = {task["id"]: task for task in doc["tasks"]}
    covers, orders = [], counted(monkeypatch, "_launch_order")
    real = freeze.covers
    monkeypatch.setattr(freeze, "covers", lambda *args: covers.append(1) or real(*args))
    started = time.process_time()
    tick_module._spawn_order("sw", store.config("sw"), store, [], rows, doc, FakeRuntime())
    spent = time.process_time() - started
    assert sum(task.get("state") == "open" for task in rows.values()) > 900
    assert len(doc["freezes"]) > 1300
    assert len(orders) == 1
    assert len(covers) < len(rows)
    assert spent < budgets.FLOORS["spawn"].floor_s / BUDGET_SHARE
