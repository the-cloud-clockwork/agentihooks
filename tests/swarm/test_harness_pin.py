from types import SimpleNamespace
from typing import Any

import pytest

NOW = 1_000_000
MINUTE = 60_000


def setup_case(pin: str, lane: str = "eng") -> tuple[Any, Any, Any]:
    import fakeredis

    from scripts.swarm import capacity
    from scripts.swarm.store import RedisStore, SwarmConfig
    from tests.swarm.test_tick import FakeLedger

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(
        SwarmConfig("sw", "/repo", max_eng=2, max_ci=2, max_plan=2, lanes={lane: {"agent": pin}})
    )
    ledger = FakeLedger(
        [
            {"id": "waiting", "title": "Repair launch", "lane": lane},
            {"id": "second", "title": "Repair claim", "lane": lane},
        ]
    )
    accounts = [capacity.Account(harness, "main", "OPEN", 0, 90, 90, 3) for harness in ("claude", "codex")]

    def quota(config: Any, agents: Any, now: float, demand: Any, requirements: Any) -> dict[str, Any]:
        return capacity.calculate(config, accounts, agents, demand)

    return store, ledger, SimpleNamespace(quota_capacity=quota)


@pytest.mark.feature("swarm-harness-pin-guard")
@pytest.mark.parametrize("pin,other", [("claude", "codex"), ("codex", "claude")])
def test_health_names_the_pin_unused_seats_waiting_tasks_and_observed_age(
    monkeypatch: pytest.MonkeyPatch, pin: str, other: str
) -> None:
    from scripts.swarm import capacity, status

    store, ledger, runtime = setup_case(pin)
    config = store.config("sw")
    capacity.apply("sw", config, store, ledger, runtime, NOW)
    capacity.apply("sw", config, store, ledger, runtime, NOW + 5 * MINUTE)
    monkeypatch.setattr(status, "now_ms", lambda: NOW + 5 * MINUTE)
    found = status.findings(store, "sw", config, ledger.tasks("sw"), [])
    pins = [row for row in found if row["kind"] == "harness pin"]
    assert len(pins) == 1
    assert pins[0]["subject"] == "eng"
    text = " ".join([pins[0]["summary"], *pins[0]["evidence"]])
    assert f"eng lane pinned to {pin}" in text
    assert f"{other} has 3 free quota seats" in text
    assert "waiting" in text and "Repair launch" in text
    assert "second" in text and "Repair claim" in text
    assert "observed pinned for 5 minutes" in text
    assert store.config("sw").lanes["eng"]["agent"] == pin


@pytest.mark.feature("swarm-harness-pin-guard")
@pytest.mark.parametrize("pin,other", [("claude", "codex"), ("codex", "claude")])
def test_quota_lines_name_the_same_pin_evidence(pin: str, other: str) -> None:
    from scripts.swarm import capacity, quota_view

    store, ledger, runtime = setup_case(pin)
    capacity.apply("sw", store.config("sw"), store, ledger, runtime, NOW)
    lines = quota_view.lines(capacity.read(store, "sw"), NOW + 5 * MINUTE, {}, "manual")
    pins = [line for line in lines if line.startswith("quota harness pin ")]
    assert len(pins) == 1
    assert f"eng lane pinned to {pin}" in pins[0]
    assert f"{other} has 3 free quota seats" in pins[0]
    assert "waiting" in pins[0] and "Repair launch" in pins[0]
    assert "second" in pins[0] and "Repair claim" in pins[0]
    assert "observed pinned for 5 minutes" in pins[0]
