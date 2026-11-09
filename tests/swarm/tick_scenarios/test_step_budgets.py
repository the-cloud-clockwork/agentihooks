"""Tick scenarios replayed on the real step clock: every step inside its floor, none abandoned, a fresh controller places."""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

from scripts.swarm import timing
from scripts.swarm.store import MASTER, RedisStore, SwarmConfig
from scripts.swarm.tick import Placed, SpawnError
from scripts.tick.steps import budgets
from tests.swarm.test_tick import FakeLedger, FakeRuntime
from tests.swarm.tick_scenarios import test_characterisation as characterisation

HIVE = "remote"
ACCOUNT = "one"


@pytest.fixture(scope="module")
def warmed(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Replay one pass first so cold imports are not charged to the first gated step."""
    with pytest.MonkeyPatch.context() as patch:
        characterisation.replay("running", "run_tick", patch, tmp_path_factory.mktemp("warm"))


def records() -> list[dict[str, Any]]:
    """Return the ``tick:records`` rows the last replay appended through its patched recorder."""
    appended = cast(Mock, timing.append_record)
    return [call.args[3] for call in appended.call_args_list if call.args[2] == "tick:records"]


def overruns(rows: list[dict[str, Any]]) -> list[str]:
    """Return one line per row whose step was abandoned or whose wall exceeds its declared floor."""
    floors = {name: budget.floor_s for name, budget in budgets.FLOORS.items()}
    if source := os.environ.get("TICK_BUDGET_FLOORS"):
        floors.update(json.loads(Path(source).read_text()))
    found: list[str] = []
    for row in rows:
        floor = floors.get(row["step"], row["budget_s"])
        if row["abandoned"]:
            found.append(f"{row['step']} abandoned after {row['wall_s']:.2f}s")
        elif row["wall_s"] > floor:
            found.append(f"{row['step']} took {row['wall_s']:.2f}s over its {floor}s floor")
    return found


@pytest.mark.feature("swarm-tick-step-budget-gate")
@pytest.mark.parametrize("scenario", characterisation.SCENARIOS)
def test_every_scenario_step_finishes_inside_its_floor(
    scenario: str, warmed: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    characterisation.replay(scenario, "run_tick", monkeypatch, tmp_path)
    rows = records()
    output = Path(os.environ.get("TICK_BUDGET_OUTPUT", str(tmp_path)))
    output.mkdir(parents=True, exist_ok=True)
    (output / f"{scenario}.json").write_text(json.dumps(rows, indent=2) + "\n")
    assert rows, f"{scenario} recorded no tick steps"
    assert not overruns(rows), f"{scenario}: " + "; ".join(overruns(rows))


@pytest.mark.feature("swarm-tick-step-budget-gate")
@pytest.mark.parametrize(
    ("row", "line"),
    [
        (
            {"step": "reap", "wall_s": 22.5, "budget_s": 22.0, "abandoned": False},
            "reap took 22.50s over its 22.0s floor",
        ),
        ({"step": "spawn", "wall_s": 44.0, "budget_s": 44.0, "abandoned": True}, "spawn abandoned after 44.00s"),
        (
            {"step": "unlisted", "wall_s": 3.0, "budget_s": 2.0, "abandoned": False},
            "unlisted took 3.00s over its 2.0s floor",
        ),
    ],
)
def test_an_overrun_or_abandoned_step_is_named(row: dict[str, Any], line: str) -> None:
    assert overruns([row]) == [line]


@pytest.mark.feature("swarm-tick-step-budget-gate")
def test_a_step_inside_its_floor_passes() -> None:
    assert overruns([{"step": "reap", "wall_s": 21.9, "budget_s": 22.0, "abandoned": False}]) == []


@pytest.mark.feature("swarm-tick-step-budget-gate")
def test_a_head_floor_increase_cannot_weaken_the_base_budget(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "floors.json"
    source.write_text(json.dumps({"reap": 22.0}))
    monkeypatch.setenv("TICK_BUDGET_FLOORS", str(source))
    monkeypatch.setattr(budgets, "FLOORS", {"reap": SimpleNamespace(floor_s=44.0)})
    assert overruns([{"step": "reap", "wall_s": 30.0, "budget_s": 44.0, "abandoned": False}]) == [
        "reap took 30.00s over its 22.0s floor"
    ]


def report_hive(store: RedisStore) -> None:
    """Register one live hive for the swarm repo with one Claude account and a fresh quota report on it."""
    from scripts.hive import registry
    from scripts.swarm import lease
    from tests.hive.test_controller_launch import publish_room

    now_ms = lease.now_ms(store)
    hive = registry._default(HIVE)
    hive.update(
        slots=[{"harness": "claude", "account": ACCOUNT, "kind": "subscription"}],
        repos=["/repo"],
        heartbeat_at=now_ms,
        max_agents=4,
    )
    store.redis.hset(registry.key(HIVE), mapping={k: registry._encode(k, v) for k, v in hive.items()})
    store.redis.sadd(registry.INDEX, HIVE)
    publish_room(store, HIVE, hive["max_agents"])
    now = now_ms / 1000
    store.redis.set(f"{registry.key(HIVE)}:sessions", json.dumps({f"claude:{ACCOUNT}": 0}), ex=45)
    quota = {
        f"claude:{ACCOUNT}": {
            "five_hour": {"used": 20, "resets_at": now + 3600},
            "seven_day": {"used": 10, "resets_at": now + 86400},
            "observed_at": now - 5,
        }
    }
    store.redis.set(f"{registry.key(HIVE)}:quota", json.dumps(quota), ex=45)


class PlacingRuntime(FakeRuntime):
    """A fake runtime whose every worker spawn first places through a fresh hive controller launcher."""

    launcher: Any = None
    selections: list[Any]

    def attach(self, store: RedisStore) -> None:
        """Build the launcher a just started controller holds: no pass quota rows, its reader over the hive reports."""
        from scripts.hive import launch
        from scripts.swarm import capacity, lease
        from scripts.swarm.profile_choice import ProfileDecision
        from scripts.swarm.runtime import HerdrRuntime

        herdr = Mock(spec=HerdrRuntime)
        herdr.pass_memo = None
        herdr.choose = Mock()
        herdr._profile_decision.side_effect = lambda config, lane, *rest: ProfileDecision(
            "engineer" if lane == "eng" else "cicd", "task", "engineer" if lane == "eng" else "cicd"
        )
        herdr.quota_accounts = capacity.hive_accounts(store)
        herdr.planned_account.return_value = None
        held = lease.acquire(store, "sw", "controller")
        assert held is not None
        self.launcher = launch.HiveLaunch(store, "sw", held, herdr)
        self.selections = []

    def spawn(self, config: SwarmConfig, lane: str, name: str, task: dict[str, Any]) -> Placed:
        """Spawn a master as the fake does; refuse a worker on its placement wait, else record the selection and spawn."""
        from scripts.hive import placement

        if lane == MASTER:
            return super().spawn(config, lane, name, task)
        selected = self.launcher.select(config, lane, task)
        if isinstance(selected, placement.Wait):
            raise SpawnError(selected.reason)
        self.selections.append(selected)
        return super().spawn(config, lane, name, task)


@pytest.mark.feature("controller-quota-after-restart")
def test_a_fresh_controller_with_an_empty_quota_cache_places_an_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from scripts import claude_quota_balancer as balancer
    from scripts.swarm import capacity

    monkeypatch.setattr(balancer, "cached_observations", lambda **kwargs: [])
    monkeypatch.setattr(capacity.account_sessions, "codex_sessions_by_account", lambda: {})
    monkeypatch.setattr(capacity.account_sessions, "sessions_by_account", lambda **kwargs: {})
    monkeypatch.setattr("hooks._redis.get_redis", lambda: None)
    monkeypatch.setattr("scripts.routing.reservation._store", lambda environ: None)
    monkeypatch.setattr("scripts.routing.reservation.current", lambda harness, environ: None)
    built: list[PlacingRuntime] = []
    prepare = characterisation.prepare

    def placing_prepare(scenario: str, store: RedisStore, ledger: FakeLedger, runtime: PlacingRuntime) -> None:
        prepare(scenario, store, ledger, runtime)
        report_hive(store)
        runtime.attach(store)
        built.append(runtime)

    monkeypatch.setattr(characterisation, "FakeRuntime", PlacingRuntime)
    monkeypatch.setattr(characterisation, "prepare", placing_prepare)
    record = characterisation.replay("running", "tick", monkeypatch, tmp_path)
    (runtime,) = built
    assert runtime.selections, f"no placement: {record['actions']}"
    assert {(chosen.hive, chosen.account.name) for chosen in runtime.selections} == {(HIVE, ACCOUNT)}
    assert all(chosen.account.cap is not None for chosen in runtime.selections)
    assert any(action.startswith("spawned engineer") for action in record["actions"][0])
