"""The autoscaler alert tick step registers with its group, lane, feature and ports, and runs the autoscaler watch
with the pass state."""

from importlib import import_module
from typing import Any

import pytest

from scripts.tick import registry


@pytest.mark.feature("swarm-autoscaler-scale-up-alert")
def test_autoscaler_alert() -> None:
    """The autoscaler alert step registers in group reopen passes on the slow lane with its feature and ports."""
    import_module("scripts.tick.steps.tasks")
    found = {step.name: step for step in registry.steps()}["autoscaler alert"]
    assert (found.group, found.lane, found.feature, found.effects) == (
        "reopen passes",
        "slow",
        "swarm-autoscaler-scale-up-alert",
        frozenset(),
    )


@pytest.mark.feature("swarm-autoscaler-scale-up-alert")
def test_the_tick_hands_the_autoscaler_watch_its_slug_store_ledger_and_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tick pass runs the autoscaler watch with the swarm's slug, store, ledger and ledger state."""
    import fakeredis

    from scripts.swarm import autoscaler_watch, tick
    from scripts.swarm.store import RedisStore, SwarmConfig
    from tests.swarm.test_tick import FakeLedger, FakeRuntime

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=0, max_ci=0))
    seen: list[tuple[Any, ...]] = []
    monkeypatch.setattr(autoscaler_watch, "alert_pass", lambda *args: seen.append(args) or ["watched the autoscaler"])
    ledger = FakeLedger([{"id": "a"}])
    actions = tick.tick("sw", store, ledger, FakeRuntime(), now_ms=4_321)
    [(slug, got_store, got_ledger, doc)] = seen
    assert (slug, got_store, got_ledger, [t["id"] for t in doc["tasks"]]) == ("sw", store, ledger, ["a"])
    assert "watched the autoscaler" in actions
