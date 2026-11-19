"""Swarm create runs the store's refusals before it marks the ledger as a swarm ledger."""

import pytest

from scripts.swarm import cli
from tests.swarm.test_cli import env as env


def test_a_create_the_store_refuses_leaves_the_ledger_unmarked(env: tuple, capsys: pytest.CaptureFixture) -> None:
    _, ledger, _ = env
    assert cli.main(["sw", "create", "--repo", "/repo"]) == 0
    ledger.swarm_sized.clear()
    capsys.readouterr()
    assert cli.main(["sw", "create", "--repo", "/repo"]) == 1
    assert capsys.readouterr().err == "swarm: swarm sw already exists\n"
    assert ledger.swarm_sized == []
