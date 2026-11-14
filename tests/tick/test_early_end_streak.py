"""Tick passes that keep ending early on the same missing value raise one health finding until a full pass."""

import json
from typing import Any

import pytest

from scripts.swarm import cli, status, tick_heartbeat
from scripts.swarm.health import early_ends
from scripts.swarm.health.findings import Finding
from scripts.tick.steps import Left, report
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_delivery import FakeHerdr

pytestmark = pytest.mark.unit


def _ending_early(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Make every pass end early at the relay site because no step stored ``value``."""

    def judged(wrapper: Any) -> bool:
        raise Left(value)

    monkeypatch.setattr(cli._Wrapper, "_judged", judged)


def _passes(swarm: Any, ledger: Any, runtime: Any, count: int) -> list[list[str]]:
    """Run ``count`` tick passes of swarm ``sw``, returning the actions of each."""
    return [cli.run_tick(swarm, "sw", ledger, runtime, FakeHerdr({})) for _ in range(count)]


def _early_ends(swarm: Any, ledger: Any) -> list[dict[str, Any]]:
    """Return the early end findings swarm status shows for ``sw``, without their verdict bookkeeping."""
    shown = status.findings(swarm, "sw", swarm.config("sw"), ledger.tasks("sw"), [])
    kept = ("kind", "subject", "summary", "evidence", "threshold")
    return [{key: row[key] for key in kept} for row in shown if row["kind"] == "early end"]


@pytest.mark.feature("swarm-tick-early-end-streak")
def test_passes_ending_early_on_one_value_raise_one_finding_at_the_threshold(
    env: tuple[Any, Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    swarm, ledger, runtime = env
    run("sw", "create", "--repo", "/repo")
    _ending_early(monkeypatch, "agents")
    _passes(swarm, ledger, runtime, 2)
    assert _early_ends(swarm, ledger) == []
    _passes(swarm, ledger, runtime, 1)
    assert tick_heartbeat.read(swarm, "sw") is not None
    assert _early_ends(swarm, ledger) == [
        {
            "kind": "early end",
            "subject": "sw",
            "summary": "Tick passes keep ending early because no step stored agents",
            "evidence": ["3 passes in a row ended early on agents while the heartbeat was stamped"],
            "threshold": "3 passes in a row",
        }
    ]


@pytest.mark.feature("swarm-tick-early-end-streak")
def test_a_full_pass_clears_the_finding(
    env: tuple[Any, Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    swarm, ledger, runtime = env
    run("sw", "create", "--repo", "/repo")
    real = cli._Wrapper._judged
    _ending_early(monkeypatch, "agents")
    early = _passes(swarm, ledger, runtime, 3)
    assert all(actions[-1].startswith("pass ended early: no step stored agents") for actions in early)
    assert len(_early_ends(swarm, ledger)) == 1
    monkeypatch.setattr(cli._Wrapper, "_judged", real)
    [full] = _passes(swarm, ledger, runtime, 1)
    assert not any(action.startswith("pass ended early") for action in full)
    assert swarm.redis.get(swarm.key("sw", "early-end-streak")) is None
    assert _early_ends(swarm, ledger) == []


@pytest.mark.feature("swarm-tick-early-end-streak")
def test_an_early_end_on_another_value_restarts_the_count(
    env: tuple[Any, Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    swarm, ledger, runtime = env
    run("sw", "create", "--repo", "/repo")
    _ending_early(monkeypatch, "agents")
    _passes(swarm, ledger, runtime, 2)
    _ending_early(monkeypatch, "judged")
    _passes(swarm, ledger, runtime, 2)
    assert _early_ends(swarm, ledger) == []


@pytest.mark.feature("swarm-tick-early-end-streak")
def test_the_health_setting_sets_the_threshold(
    env: tuple[Any, Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    swarm, ledger, runtime = env
    run("sw", "create", "--repo", "/repo")
    monkeypatch.setenv("AGENTIHOOKS_HEALTH_EARLY_ENDS", "5")
    _ending_early(monkeypatch, "agents")
    _passes(swarm, ledger, runtime, 4)
    assert _early_ends(swarm, ledger) == []
    _passes(swarm, ledger, runtime, 1)
    assert [row["threshold"] for row in _early_ends(swarm, ledger)] == ["5 passes in a row"]


@pytest.fixture
def store() -> Any:
    """Return a swarm store on a fresh fake Redis."""
    import fakeredis

    from scripts.swarm.store import RedisStore

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


@pytest.mark.feature("swarm-tick-early-end-streak")
@pytest.mark.parametrize(
    ("environ", "limit"),
    [({}, 3), ({"AGENTIHOOKS_HEALTH_EARLY_ENDS": "5"}, 5), ({"AGENTIHOOKS_HEALTH_EARLY_ENDS": "1"}, 1)]
    + [({"AGENTIHOOKS_HEALTH_EARLY_ENDS": raw}, 3) for raw in ("0", "-2", "x", "", "²")],
)
def test_the_threshold_takes_only_a_positive_whole_number(environ: dict[str, str], limit: int) -> None:
    assert early_ends.threshold(environ) == limit


@pytest.mark.feature("swarm-tick-early-end-streak")
def test_the_streak_is_stored_under_its_key_and_a_single_end_meets_a_threshold_of_one(store: Any) -> None:
    one = {"AGENTIHOOKS_HEALTH_EARLY_ENDS": "1"}
    assert early_ends.findings(store, "sw", one) == []
    early_ends.note(store, "sw", "agents")
    assert json.loads(store.redis.get(store.key("sw", "early-end-streak"))) == {"value": "agents", "count": 1}
    assert early_ends.findings(store, "sw", one) == [
        Finding(
            "early end",
            "sw",
            "Tick passes keep ending early because no step stored agents",
            ("1 passes in a row ended early on agents while the heartbeat was stamped",),
            "1 passes in a row",
            1,
        )
    ]
    early_ends.clear(store, "sw")
    assert store.redis.get(store.key("sw", "early-end-streak")) is None


@pytest.mark.feature("swarm-tick-early-end-streak")
def test_each_early_end_of_the_report_step_counts_the_missing_value(store: Any) -> None:
    report.early_end(store, "sw", Left("agents"), [])
    report.early_end(store, "sw", Left("agents"), [])
    assert early_ends.read(store, "sw") == {"value": "agents", "count": 2}


@pytest.mark.feature("swarm-tick-early-end-streak")
def test_a_pass_whose_lane_dropped_the_page_relay_still_lists_its_agents_and_ends_full(
    env: tuple[Any, Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.swarm.store import AgentRecord

    swarm, ledger, runtime = env
    run("sw", "create", "--repo", "/repo")
    swarm.put_agent("sw", AgentRecord("engineer@a1b2c3-0009", "eng", "t1", state="finished"))
    real = cli._Wrapper.shut
    wrappers: list[Any] = []

    def shut(wrapper: Any, step: Any) -> bool:
        wrappers.append(wrapper)
        return step.name == "page relay" or real(wrapper, step)

    monkeypatch.setattr(cli._Wrapper, "shut", shut)
    [actions] = _passes(swarm, ledger, runtime, 1)
    assert not any(action.startswith("pass ended early") for action in actions)
    wrapper = wrappers[-1]
    assert not wrapper.seen["settle"].abandoned
    assert wrapper.seen["page relay"].rested
    unfinished = [agent.name for agent in swarm.agents("sw") if agent.state != "finished"]
    assert [agent.name for agent in wrapper.box["agents"]] == unfinished
    assert "engineer@a1b2c3-0009" not in unfinished
    assert swarm.redis.get(swarm.key("sw", "early-end-streak")) is None
