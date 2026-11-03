"""Tick passes that keep ending early on the same missing value raise one health finding until a full pass."""

from typing import Any

import pytest

from scripts.swarm import cli, status, tick_heartbeat
from scripts.tick.steps import Left
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_delivery import FakeHerdr

pytestmark = pytest.mark.unit


def _ending_early(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Make every pass end early at the relay group because no step stored ``value``."""
    real = cli.run_group

    def group(name: str, **state: Any) -> list[str]:
        if name == "wrapper relay":
            raise Left(value)
        return real(name, **state)

    monkeypatch.setattr(cli, "run_group", group)


def _passes(swarm: Any, ledger: Any, runtime: Any, count: int) -> None:
    """Run ``count`` tick passes of swarm ``sw``."""
    for _ in range(count):
        cli.run_tick(swarm, "sw", ledger, runtime, FakeHerdr({}))


def _early_ends(swarm: Any, ledger: Any) -> list[dict[str, Any]]:
    """Return the early end findings swarm status shows for ``sw``."""
    found = status.findings(swarm, "sw", swarm.config("sw"), ledger.tasks("sw"), [])
    return [finding.as_dict() for finding in found if finding.kind == "early end"]


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
    real = cli.run_group
    _ending_early(monkeypatch, "agents")
    _passes(swarm, ledger, runtime, 3)
    assert len(_early_ends(swarm, ledger)) == 1
    monkeypatch.setattr(cli, "run_group", real)
    _passes(swarm, ledger, runtime, 1)
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
