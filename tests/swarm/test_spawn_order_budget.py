"""The spawn order computes its claim order once per pass and fits its budget over a replayed swarm ledger."""

import gzip
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from scripts.swarm import fleet_red, freeze, release_wait
from scripts.swarm import tick as tick_module
from scripts.swarm.store import RedisStore
from scripts.tick.steps import budgets
from tests.swarm import test_tick

pytestmark = pytest.mark.unit
store = test_tick.store
FakeLedger, FakeRuntime = test_tick.FakeLedger, test_tick.FakeRuntime
REPLAY = Path(__file__).with_name("spawn_pass_replay.json.gz")
BUDGET_SHARE = 10


def replay() -> dict[str, Any]:
    """A swarm ledger's structure: 4827 tasks, 997 open, 1322 freeze and focus records."""
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


def scanned(records: list[dict[str, Any]], verb: str, task: dict[str, Any], chain: list[str]) -> int:
    """The nearest covering level as a scan over every record finds it."""
    targets = [r["target"] for r in records if r["verb"] == verb]
    levels = [chain.index(t) if t in chain else len(chain) for t in targets if freeze.covers(t, task, chain)]
    return min(levels, default=sys.maxsize)


@pytest.mark.feature("swarm-spawn-order-once-per-pass")
def test_a_spawn_pass_computes_its_claim_order_once_for_every_lane(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    assert sorted(task["id"] for _, task in order) == ["t1", "t2", "t3", "t4"]


@pytest.mark.feature("swarm-spawn-order-once-per-pass")
def test_a_spawn_pass_over_the_replayed_ledger_orders_claims_once(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    offline(monkeypatch)
    doc = replay()
    rows = {task["id"]: task for task in doc["tasks"]}
    covers, orders = [], counted(monkeypatch, "_launch_order")
    real = freeze.covers
    monkeypatch.setattr(freeze, "covers", lambda *args: covers.append(1) or real(*args))
    tick_module._spawn_order("sw", store.config("sw"), store, [], rows, doc, FakeRuntime())
    assert sum(task.get("state") == "open" for task in rows.values()) > 900
    assert len(doc["freezes"]) > 1300
    assert len(orders) == 1
    assert len(covers) < len(rows)


@pytest.mark.feature("swarm-spawn-order-once-per-pass")
@pytest.mark.skipif("MUTANT_UNDER_TEST" in os.environ, reason="mutation trampolines slow every call")
def test_a_spawn_pass_over_the_replayed_ledger_finishes_inside_a_tenth_of_the_spawn_floor(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    offline(monkeypatch)
    doc = replay()
    rows = {task["id"]: task for task in doc["tasks"]}
    started = time.process_time()
    tick_module._spawn_order("sw", store.config("sw"), store, [], rows, doc, FakeRuntime())
    assert time.process_time() - started < budgets.FLOORS["spawn"].floor_s / BUDGET_SHARE


@pytest.mark.feature("swarm-spawn-order-once-per-pass")
@pytest.mark.parametrize("verb", ["freeze", "focus"])
def test_the_indexed_freeze_targets_find_the_level_a_full_scan_finds(verb: str) -> None:
    other = "freeze" if verb == "focus" else "focus"
    targets = ("lane:eng", "kind:ops", "lane:", "kind", "tasks/t1", "phases/p1", "plans/q", "lane:ci")
    records = [{"verb": verb, "target": target} for target in targets] + [{"verb": other, "target": "tasks/t2"}]
    tasks = [
        ({"id": "t1", "lane": "eng"}, ["tasks/t1", "slices/s", "phases/p1", "plans/q"]),
        ({"id": "t2", "lane": "plan", "kind": "ops"}, ["tasks/t2", "phases/p2"]),
        ({"id": "t3", "lane": "plan"}, ["tasks/t3", "lane:", "phases/p1"]),
        ({"id": "t4", "lane": "ci"}, ["tasks/t4"]),
        ({"id": "t5", "lane": "plan"}, ["tasks/t5", "phases/p9"]),
    ]
    for task, chain in tasks:
        assert freeze._nearest(records, verb, task, chain) == scanned(records, verb, task, chain)
