"""A tick pass that ends early on a pass value no step stored names that value in its log."""

from typing import Any

import pytest

from scripts.swarm import cli
from tests.swarm.test_cli import env, run
from tests.swarm.test_delivery import FakeHerdr

__all__ = ["env"]


def test_a_pass_that_ends_early_on_a_missing_value_logs_the_value(
    env: tuple[Any, Any, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Skipping the relay group leaves the agents value unset, so the pass returns no actions and logs it."""
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    real_run_group = cli.run_group

    def skip_relay(name: str, **kwargs: Any) -> Any:
        return [] if name == "wrapper relay" else real_run_group(name, **kwargs)

    monkeypatch.setattr(cli, "run_group", skip_relay)
    capsys.readouterr()
    assert cli.run_tick(store, "sw", ledger, rt, FakeHerdr({})) == []
    assert "sw: pass ended early: no step stored 'agents'" in capsys.readouterr().err
