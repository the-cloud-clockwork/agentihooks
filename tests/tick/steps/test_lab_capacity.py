"""The lab capacity tick step reads the configured lab into the swarm lab block and ships its rows."""

import json
from importlib import import_module
from typing import Any

import pytest

from scripts.swarm import lab_capacity
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.tick import registry

NOW = 1_800_000_000_000
DOC = {
    "classes": {"queue": {"pending": 2, "running": 1, "predicted_wait_s": 20.0}},
    "slots": {"total": 4, "busy": 1, "free": 3},
    "utilisation": 0.25,
    "budget_fraction": 1.0,
    "shed": [],
}


@pytest.fixture
def store() -> RedisStore:
    import fakeredis

    found = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    found.create(SwarmConfig("sw", "/repo", 1, 1))
    return found


def run_step(store: RedisStore) -> Any:
    observe: Any = import_module("scripts.tick.steps.observe")
    state = {"slug": "sw", "store": store, "now_ms": NOW}
    return observe._lab_capacity(None, state.__getitem__)


@pytest.mark.feature("lab-capacity-reader")
def test_lab_capacity() -> None:
    """The lab capacity step registers in group observe on the slow lane with its feature and ports."""
    import_module("scripts.tick.steps.observe")
    found = {step.name: step for step in registry.steps()}["lab capacity"]
    assert (found.group, found.lane, found.feature, found.effects) == (
        "observe",
        "slow",
        "lab-capacity-reader",
        frozenset({"metrics"}),
    )


@pytest.mark.feature("lab-capacity-reader")
def test_the_step_keeps_the_lab_block_and_records_its_rows(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> None:
    metrics: Any = import_module("scripts.swarm.metrics")
    recorded: list[tuple[Any, list[dict[str, Any]], int]] = []
    monkeypatch.setattr(lab_capacity, "read", lambda url: DOC)
    monkeypatch.setattr(
        metrics, "record", lambda table, rows, now_ms: recorded.append((table, rows, now_ms)) or ["kept"]
    )
    store.update("sw", lab_url="http://lab:8800")
    assert run_step(store) == ["kept"]
    assert recorded == [(lab_capacity.TABLE, lab_capacity.rows("sw", DOC, NOW), NOW)]
    assert json.loads(store.redis.get(store.key("sw", "lab-capacity")))["capacity"] == DOC


@pytest.mark.feature("lab-capacity-reader")
def test_the_step_reads_nothing_without_a_lab_url(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> None:
    metrics: Any = import_module("scripts.swarm.metrics")
    monkeypatch.setattr(lab_capacity, "read", lambda url: pytest.fail("read without a lab url"))
    monkeypatch.setattr(metrics, "record", lambda table, rows, now_ms: pytest.fail("recorded without rows"))
    assert run_step(store) == []
    assert lab_capacity.status(store, "sw") is None
